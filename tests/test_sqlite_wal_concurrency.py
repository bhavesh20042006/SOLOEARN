from __future__ import annotations

"""
tests/test_sqlite_wal_concurrency.py
======================================
Comprehensive verification suite for SQLite WAL concurrency, automated database maintenance,
structured logging with log rotation, daemon resilience with exponential backoff, and health telemetry.
"""

import json
import logging
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import core.config
from core.config import DATA_DIR, LOGS_DIR, ensure_directories
from core.db import (
    add_opportunity,
    connect,
    create_invoice,
    create_task,
    execute_with_retry,
    get_opportunity,
    init_db,
    update_opportunity_status,
    utc_now,
)
from core.db_maintenance import (
    BACKUPS_DIR,
    DatabaseMaintenanceManager,
    backup_database,
    check_database_integrity,
    checkpoint_wal,
    get_database_stats,
    prune_old_backups,
    vacuum_database,
)
from scripts.run_100_percent_autonomous_daemon import (
    AutonomousCycleMetrics,
    AutonomousMasterEngine,
    DaemonHealthState,
    ExponentialBackoff,
    accumulate_lifetime_metrics,
    is_network_error,
    log_daemon_event,
    read_health_telemetry,
    setup_daemon_logging,
    update_health_telemetry,
)


class SQLiteWALConcurrencyTests(unittest.TestCase):
    """Rigorous multi-threaded stress tests verifying SQLite WAL concurrency without locking failures."""

    def setUp(self):
        ensure_directories()
        init_db()

    def test_multi_threaded_concurrent_reads_and_writes_no_locks(self):
        """
        Spawns 12 concurrent worker threads actively writing, updating, and reading records.
        Verifies that with WAL mode, busy_timeout=60000, and execute_with_retry,
        zero 'database is locked' errors occur.
        """
        num_threads = 12
        iterations_per_thread = 15
        errors: list[Exception] = []
        created_opp_ids: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(num_threads)

        def worker(thread_idx: int):
            barrier.wait(timeout=10)
            for i in range(iterations_per_thread):
                try:
                    # Write: Add opportunity
                    opp_id = add_opportunity(
                        {
                            "title": f"Stress Thread {thread_idx} Iteration {i}",
                            "source": "Concurrency Stress",
                            "url": "https://example.com/stress",
                            "description": "Multi-threaded WAL stress test record",
                            "amount_inr": 5000.0 + (thread_idx * 100) + i,
                            "estimated_hours": 2.0,
                            "skill_match": 0.95,
                            "payment_confidence": 0.95,
                            "risk_level": 1.0,
                            "source_permitted": True,
                            "is_sandbox": True,
                        },
                        score=95.0,
                        is_sandbox=True,
                    )
                    with lock:
                        created_opp_ids.append(opp_id)

                    # Update: Modify status
                    update_opportunity_status(opp_id, "shortlisted")

                    # Read: Fetch opportunity
                    opp = get_opportunity(opp_id)
                    self.assertIsNotNone(opp)
                    self.assertEqual(opp["status"], "shortlisted")

                    # Task creation linked to opportunity
                    task_id = create_task(
                        title=f"Task for Opp {opp_id}",
                        task_type="data_analytics",
                        workspace=f"task_{opp_id}",
                        opportunity_id=opp_id,
                        is_sandbox=True,
                    )
                    self.assertGreater(task_id, 0)

                except Exception as exc:
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(
            len(errors),
            0,
            f"Multi-threaded stress encountered {len(errors)} locking/execution errors: {errors[:3]}",
        )
        self.assertEqual(len(created_opp_ids), num_threads * iterations_per_thread)

    def test_concurrent_wal_checkpointing_during_heavy_writes(self):
        """
        Executes active WAL checkpoints (both TRUNCATE and PASSIVE) while concurrent writer
        threads are continually inserting and committing transactions.
        """
        stop_event = threading.Event()
        writer_errors: list[Exception] = []
        checkpoint_results: list[dict] = []
        lock = threading.Lock()

        def continuous_writer():
            w_idx = 0
            while not stop_event.is_set():
                w_idx += 1
                try:
                    with connect() as conn:
                        conn.execute(
                            "INSERT INTO audit_log (event, details, created_at) VALUES (?, ?, ?)",
                            (f"stress_write_{w_idx}", "concurrent WAL test payload", utc_now()),
                        )
                    time.sleep(0.01)
                except Exception as ex:
                    with lock:
                        writer_errors.append(ex)

        writers = [threading.Thread(target=continuous_writer) for _ in range(4)]
        for w in writers:
            w.start()

        try:
            # Perform multiple checkpoints while writers are writing
            for mode in ["PASSIVE", "TRUNCATE", "PASSIVE"]:
                res = checkpoint_wal(mode)
                checkpoint_results.append(res)
                self.assertIn("log_pages", res)
                self.assertIn("checkpointed_pages", res)
                time.sleep(0.1)
        finally:
            stop_event.set()
            for w in writers:
                w.join(timeout=5)

        self.assertEqual(len(writer_errors), 0, f"Writers failed during checkpoint: {writer_errors[:3]}")
        self.assertGreaterEqual(len(checkpoint_results), 3)

    def test_concurrent_online_backup_during_active_transactions(self):
        """
        Executes online non-blocking atomic backup using SQLite's native backup API
        while concurrent transactions are actively writing to the database.
        Verifies backup integrity with PRAGMA quick_check.
        """
        stop_event = threading.Event()
        writer_errors: list[Exception] = []

        def writer():
            count = 0
            while not stop_event.is_set():
                count += 1
                try:
                    with connect() as conn:
                        conn.execute(
                            "INSERT INTO audit_log (event, details, created_at) VALUES (?, ?, ?)",
                            (f"backup_stress_{count}", "data written during live backup", utc_now()),
                        )
                    time.sleep(0.005)
                except Exception as ex:
                    writer_errors.append(ex)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_backup_dir = Path(tmp_dir)
                b_path = backup_database(backup_dir=tmp_backup_dir, keep_count=3, verify_after=True)
                self.assertTrue(b_path.exists())
                self.assertGreater(b_path.stat().st_size, 0)

                # Verify backup database file integrity
                b_conn = sqlite3.connect(b_path)
                try:
                    res = b_conn.execute("PRAGMA quick_check").fetchall()
                    self.assertEqual(res, [("ok",)])
                finally:
                    b_conn.close()
        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(len(writer_errors), 0, f"Writers encountered errors during backup: {writer_errors}")

    def test_execute_with_retry_recovers_from_transient_busy(self):
        """Verifies that execute_with_retry recovers from transient OperationalError."""
        attempts = 0

        def flaky_operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return "success"

        result = execute_with_retry(flaky_operation, max_retries=5, initial_delay=0.01)
        self.assertEqual(result, "success")
        self.assertEqual(attempts, 3)


class DatabaseMaintenanceManagerTests(unittest.TestCase):
    """Tests automated SQLite database maintenance, vacuuming, backups, and retention."""

    def setUp(self):
        ensure_directories()
        init_db()

    def test_database_stats_reporting(self):
        stats = get_database_stats()
        self.assertGreater(stats.db_size_bytes, 0)
        self.assertGreater(stats.page_count, 0)
        self.assertEqual(stats.page_size, 4096)
        self.assertTrue(stats.integrity_ok)
        self.assertEqual(stats.integrity_messages, ["ok"])
        d = stats.to_dict()
        self.assertIn("db_size_bytes", d)
        self.assertIn("total_backups", d)

    def test_vacuum_database_execution(self):
        v_ok = vacuum_database(max_retries=3, retry_delay=0.1)
        self.assertTrue(v_ok)

    def test_backup_pruning_retention(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            p_dir = Path(tmp_dir)
            # Create 5 mock backup files
            for i in range(5):
                f = p_dir / f"soloearn_backup_20261113_00000{i}.sqlite3"
                f.write_text("test_backup_data", encoding="utf-8")
                time.sleep(0.01)

            # Keep 3 backups, should prune 2 oldest
            removed = prune_old_backups(p_dir, keep_count=3)
            self.assertEqual(len(removed), 2)
            remaining = list(p_dir.glob("soloearn_backup_*.sqlite3"))
            self.assertEqual(len(remaining), 3)

    def test_maintenance_manager_schedule_and_force(self):
        mgr = DatabaseMaintenanceManager(
            checkpoint_interval_seconds=10,
            backup_interval_seconds=10,
            vacuum_interval_seconds=10,
        )
        res = mgr.run_maintenance_if_due(force=True)
        self.assertIn("checkpoint", res)
        self.assertIn("backup", res)
        self.assertIn("vacuum", res)
        self.assertIn("stats", res)
        self.assertIn("summary", res)

        summary = mgr.get_summary()
        self.assertEqual(summary["total_checkpoints"], 1)
        self.assertEqual(summary["total_backups"], 1)
        self.assertEqual(summary["total_vacuums"], 1)
        self.assertIsNotNone(summary["last_checkpoint_utc"])


class DaemonResilienceAndTelemetryTests(unittest.TestCase):
    """Tests exponential backoff, zero-crash recovery, structured logging, and health telemetry."""

    def test_exponential_backoff_calculation(self):
        backoff = ExponentialBackoff(base_delay=5.0, max_delay=120.0, factor=2.0, jitter_pct=0.1)

        d0 = backoff.compute_delay(0)
        self.assertEqual(d0, 0.0)

        d1 = backoff.compute_delay(1)
        self.assertGreaterEqual(d1, 5.0)
        self.assertLessEqual(d1, 5.5)

        d2 = backoff.compute_delay(2)
        self.assertGreaterEqual(d2, 10.0)
        self.assertLessEqual(d2, 11.0)

        d3 = backoff.compute_delay(3)
        self.assertGreaterEqual(d3, 20.0)
        self.assertLessEqual(d3, 22.0)

        # Max delay bound
        d_extreme = backoff.compute_delay(15)
        self.assertLessEqual(d_extreme, 132.0)  # max_delay + 10% jitter

    def test_is_network_error_detection(self):
        self.assertTrue(is_network_error(ConnectionResetError("Connection reset by peer")))
        self.assertTrue(is_network_error(TimeoutError("Request timed out")))
        self.assertTrue(is_network_error(socket.gaierror(-2, "Name or service not known")))
        self.assertTrue(is_network_error(Exception("Failed to establish connection: getaddrinfo failed")))
        self.assertFalse(is_network_error(ValueError("Invalid argument value")))
        self.assertFalse(is_network_error(KeyError("missing_field")))

    def test_structured_logging_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log_dir = Path(tmp_dir) / "logs"
            # Set small max_bytes to verify rotation
            max_b = 500
            backups = 3

            import scripts.run_100_percent_autonomous_daemon as daemon_mod
            with patch.object(daemon_mod, "LOGS_DIR", test_log_dir), \
                 patch.object(daemon_mod, "DAEMON_LOG_FILE", test_log_dir / "test_daemon.log"), \
                 patch.object(daemon_mod, "DAEMON_EVENTS_FILE", test_log_dir / "test_events.jsonl"):

                d_logger, e_logger = daemon_mod.setup_daemon_logging(
                    log_level=logging.INFO,
                    max_bytes=max_b,
                    backup_count=backups,
                )

                try:
                    # Write enough messages to trigger file rotation
                    for i in range(25):
                        daemon_mod.log_daemon_event("stress_event", {"index": i, "payload": "A" * 50})
                        d_logger.info(f"Log line {i}: " + "B" * 50)

                    # Verify rotated files exist
                    event_files = list(test_log_dir.glob("test_events.jsonl*"))
                    log_files = list(test_log_dir.glob("test_daemon.log*"))

                    self.assertGreater(len(event_files), 1, "Events log should have rotated")
                    self.assertGreater(len(log_files), 1, "Daemon log should have rotated")

                    # Verify JSON formatting of event lines
                    main_events = (test_log_dir / "test_events.jsonl").read_text(encoding="utf-8")
                    lines = [l.strip() for l in main_events.splitlines() if l.strip()]
                    for line in lines:
                        parsed = json.loads(line)
                        self.assertIn("timestamp", parsed)
                        self.assertIn("event", parsed)
                        self.assertIn("pid", parsed)
                finally:
                    # Close and detach all handlers to release Windows file locks
                    for h in list(logging.getLogger().handlers):
                        h.close()
                        logging.getLogger().removeHandler(h)
                    for h in list(e_logger.handlers):
                        h.close()
                        e_logger.removeHandler(h)

    def test_health_telemetry_atomic_writing_and_reading(self):
        state = DaemonHealthState(
            status="running",
            pid=os.getpid(),
            mode="sandbox",
            started_at_utc=utc_now(),
            uptime_seconds=42.5,
            current_cycle=3,
            consecutive_failures=0,
            last_cycle_metrics={"revenue_earned_inr": 5000.0},
        )
        update_health_telemetry(state)

        telemetry = read_health_telemetry()
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["status"], "running")
        self.assertEqual(telemetry["current_cycle"], 3)
        self.assertEqual(telemetry["consecutive_failures"], 0)
        self.assertEqual(telemetry["last_cycle_metrics"]["revenue_earned_inr"], 5000.0)

    def test_autonomous_master_engine_cycle_resilience(self):
        """Verifies that an autonomous cycle handles maintenance, order intake, and settlement safely."""
        engine = AutonomousMasterEngine(is_sandbox=True)
        metrics = engine.run_complete_cycle()

        self.assertIsInstance(metrics, AutonomousCycleMetrics)
        self.assertGreaterEqual(metrics.tasks_executed, 0)
        self.assertGreaterEqual(metrics.wal_checkpoints, 0)
        self.assertGreaterEqual(metrics.revenue_earned_inr, 0.0)

        # Update telemetry and verify
        state = DaemonHealthState(
            status="sleeping",
            pid=os.getpid(),
            mode="sandbox",
            current_cycle=1,
            last_cycle_metrics=metrics.to_dict(),
        )
        accumulate_lifetime_metrics(state, metrics)
        update_health_telemetry(state)

        read_back = read_health_telemetry()
        self.assertEqual(read_back["status"], "sleeping")
        self.assertEqual(read_back["current_cycle"], 1)


if __name__ == "__main__":
    unittest.main()
