from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import ensure_directories
from core.db import (
    add_opportunity,
    claim_opportunity_for_execution,
    connect,
    get_opportunity,
    release_opportunity_execution_claim,
    create_invoice,
    create_task,
    init_db,
    add_revenue,
    update_opportunity_status,
    verify_invoice_payment,
)
from core.invoice_generator import render_invoice_html
from core.autonomous_worker import run_autonomous_job
from core.scheduler import AutonomousDaemonScheduler
from core.service_executors import (
    execute_business_automation_service,
    execute_dashboard_development_service,
    execute_data_engineering_service,
    safe_relative_write,
)


class Milestone5HardeningTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM revisions")
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM execution_claims")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM discovery_sources")
            conn.execute("DELETE FROM audit_log")

    def _submitted_opportunity_with_task(self, title: str) -> tuple[int, int]:
        opportunity_id = add_opportunity(
            {
                "title": title,
                "source": "Direct client",
                "url": "https://example.com/project",
                "description": "Approved CSV reconciliation deliverable.",
                "amount_inr": 10000.0,
                "estimated_hours": 4.0,
                "skill_match": 0.9,
                "payment_confidence": 0.9,
                "risk_level": 1.0,
                "source_permitted": True,
            },
            score=90.0,
        )
        task_id = create_task("Verified delivery", "csv_reconciliation", "workspace", opportunity_id)
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?",
                (task_id,),
            )
        for status in ("approved_for_work", "in_progress", "submission_ready", "submitted_by_owner"):
            update_opportunity_status(opportunity_id, status)
        return opportunity_id, task_id

    def test_csv_services_reject_empty_or_malformed_input_without_sample_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for executor in (
                lambda p, data: execute_data_engineering_service(p, data, "client.csv"),
                lambda p, data: execute_dashboard_development_service(p, data, "client.csv"),
                lambda p, data: execute_business_automation_service(p, data, "client.csv", "Reconcile"),
            ):
                with self.subTest(executor=executor.__name__ if hasattr(executor, "__name__") else "service"):
                    with self.assertRaises(ValueError):
                        executor(root, b"")
                    with self.assertRaises(ValueError):
                        executor(root, b'a,b\n"unterminated,quote\n')

    def test_dashboard_escapes_untrusted_filename_headers_and_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = b'<img src=x onerror=alert(1)>,amount\n<script>alert("cell")</script>,10\n'
            execute_dashboard_development_service(
                root,
                data,
                'evil"><script>alert(2)</script>.csv',
            )
            html = (root / "dashboard.html").read_text(encoding="utf-8")
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("&lt;img", html)
            self.assertNotIn('<img src=x onerror=', html)
            self.assertNotIn('<script>alert("cell")</script>', html)
            self.assertNotIn('<script>alert(2)</script>', html)

    def test_safe_relative_write_rejects_windows_and_posix_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "workspace"
            root.mkdir()
            for unsafe in ("../outside.txt", r"..\outside.txt", "C:\\outside.txt", "/tmp/outside.txt"):
                with self.subTest(path=unsafe):
                    with self.assertRaises(ValueError):
                        safe_relative_write(root, unsafe, "blocked")

    def test_safe_relative_write_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "workspace"
            outside = Path(folder) / "outside"
            base.mkdir()
            outside.mkdir()
            link = base / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is unavailable in this environment")
            with self.assertRaises(ValueError):
                safe_relative_write(base, "linked/escape.txt", "blocked")
            self.assertFalse((outside / "escape.txt").exists())

    def test_generated_data_pipeline_escapes_formula_strings_but_preserves_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "input.csv"
            output = root / "pipeline_output.csv"
            source.write_text(
                'label,amount\n=HYPERLINK(1),-17.5\n-2+3,+100\n',
                encoding="utf-8",
            )
            execute_data_engineering_service(root, source.read_bytes(), "input.csv")
            result = subprocess.run(
                [sys.executable, str(root / "pipeline.py"), "--input", str(source), "--output", str(output)],
                cwd=root, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output_text = output.read_text(encoding="utf-8")
            self.assertIn("'=HYPERLINK", output_text)
            self.assertIn("'-2+3", output_text)
            self.assertIn("-17.5", output_text)
            self.assertIn("100", output_text)

    def test_generated_reconciliation_script_escapes_formula_strings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "input.csv"
            output = root / "reconciled.csv"
            summary = root / "summary.json"
            source.write_text(
                'label,amount\n=HYPERLINK(1),-17.5\n-2+3,+100\n',
                encoding="utf-8",
            )
            execute_business_automation_service(root, source.read_bytes(), "input.csv", "Reconcile")
            result = subprocess.run(
                [sys.executable, str(root / "automation.py"), "--input", str(source), "--output", str(output), "--summary", str(summary)],
                cwd=root, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output_text = output.read_text(encoding="utf-8")
            self.assertIn("'=HYPERLINK", output_text)
            self.assertIn("'-2+3", output_text)
            self.assertIn("-17.5", output_text)
            self.assertIn("100", output_text)

    def test_failed_secondary_worker_does_not_release_primary_worker_claim(self):
        opportunity_id = add_opportunity(
            {
                "title": "Concurrent worker ownership guard",
                "source": "Direct client",
                "url": "https://example.com/lease",
                "description": "CSV analysis",
                "amount_inr": 5000,
                "estimated_hours": 2,
                "skill_match": 0.9,
                "payment_confidence": 0.9,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=90,
        )
        update_opportunity_status(opportunity_id, "approved_for_work")
        self.assertTrue(claim_opportunity_for_execution(opportunity_id))
        authoritative = get_opportunity(opportunity_id)
        self.assertIsNotNone(authoritative)
        with self.assertRaises(RuntimeError):
            run_autonomous_job(authoritative, b"id,value\n1,10\n", "input.csv")
        with connect() as conn:
            claim = conn.execute(
                "SELECT opportunity_id FROM execution_claims WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
            status = conn.execute(
                "SELECT status FROM opportunities WHERE id=?",
                (opportunity_id,),
            ).fetchone()["status"]
        self.assertIsNotNone(claim, "A rejected secondary worker must not delete the primary worker's lock")
        self.assertEqual(status, "in_progress", "A rejected secondary worker must not alter primary execution state")
        release_opportunity_execution_claim(opportunity_id)

    def test_manual_revenue_entry_cannot_mark_paid(self):
        opportunity_id, _task_id = self._submitted_opportunity_with_task("Paid ledger protection")
        with self.assertRaisesRegex(ValueError, "invoice workflow"):
            add_revenue("Manually entered paid receipt", 1000, "paid", opportunity_id=opportunity_id)

    def test_ranked_opportunities_include_risk_adjusted_gross_yield(self):
        items = [
            {"title": "A", "amount_inr": 10000, "estimated_hours": 5, "payment_confidence": 0.8, "skill_match": 0.9, "risk_level": 1},
            {"title": "B", "amount_inr": 5000, "estimated_hours": 2, "payment_confidence": 0.9, "skill_match": 0.8, "risk_level": 2},
        ]
        ranked = __import__("core.revenue_optimizer", fromlist=["RevenueOptimizer"]).RevenueOptimizer.rank_opportunities_by_hourly_yield(items)
        self.assertTrue(all("risk_adjusted_gross_yield_inr_per_hour" in item for item in ranked))
        self.assertGreaterEqual(ranked[0]["risk_adjusted_gross_yield_inr_per_hour"], ranked[1]["risk_adjusted_gross_yield_inr_per_hour"])
        self.assertNotIn("risk_adjusted_gross_yield_inr_per_hour", items[0])

    def test_generated_csv_automation_script_really_executes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = b"Order ID,Amount\n1,100\n1,100\n2,250\n"
            execute_business_automation_service(root, source, "orders.csv", "Order reconciliation")
            input_path = root / "runtime_input.csv"
            output_path = root / "runtime_output.csv"
            summary_path = root / "runtime_summary.json"
            input_path.write_bytes(source)
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "automation.py"),
                    "--input", str(input_path),
                    "--output", str(output_path),
                    "--summary", str(summary_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output_path.is_file())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["input_rows"], 3)
            self.assertEqual(summary["output_rows"], 2)
            self.assertEqual(summary["duplicates_removed"], 1)

    def test_scheduler_run_forever_performs_repeated_cycles_and_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            scheduler = AutonomousDaemonScheduler(heartbeat_path=Path(folder) / "heartbeat.json")
            calls = []

            class TestStopEvent:
                waits = 0

                def is_set(self):
                    return False

                def wait(self, _seconds):
                    self.waits += 1
                    return self.waits >= 2

            stop = TestStopEvent()

            def fake_run_once():
                calls.append(len(calls) + 1)

            scheduler.run_once = fake_run_once  # type: ignore[method-assign]
            scheduler.run_forever(interval_seconds=10, stop_event=stop)  # type: ignore[arg-type]
            self.assertEqual(len(calls), 2)
            self.assertFalse(scheduler.is_running)

    def _create_in_progress_claimed_task(self, title: str) -> tuple[int, int]:
        opportunity_id = add_opportunity(
            {
                "title": title, "source": "Direct client", "url": "https://example.com/task",
                "description": "CSV analysis", "amount_inr": 5000, "estimated_hours": 2,
                "skill_match": 0.9, "payment_confidence": 0.9, "risk_level": 1, "source_permitted": True,
            },
            score=90,
        )
        update_opportunity_status(opportunity_id, "approved_for_work")
        task_id = create_task(title, "data_analytics", "workspace", opportunity_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (task_id,))
        self.assertTrue(claim_opportunity_for_execution(opportunity_id))
        return opportunity_id, task_id

    def test_scheduler_does_not_recover_task_with_fresh_execution_lease(self):
        opportunity_id, _task_id = self._create_in_progress_claimed_task("Live worker lease")
        recovered = __import__("core.db", fromlist=["recover_interrupted_tasks"]).recover_interrupted_tasks()
        self.assertNotIn(opportunity_id, recovered)
        with connect() as conn:
            status = conn.execute("SELECT status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()["status"]
            lease = conn.execute("SELECT 1 FROM execution_claims WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        self.assertEqual(status, "in_progress")
        self.assertIsNotNone(lease)

    def test_scheduler_reclaims_stale_execution_lease_and_flags_task(self):
        opportunity_id, task_id = self._create_in_progress_claimed_task("Stale worker lease")
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute("UPDATE execution_claims SET claimed_at=? WHERE opportunity_id=?", (stale_at, opportunity_id))
        from core.db import recover_interrupted_tasks
        recovered = recover_interrupted_tasks(stale_after_seconds=6 * 60 * 60)
        self.assertIn(opportunity_id, recovered)
        with connect() as conn:
            opp_status = conn.execute("SELECT status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()["status"]
            task_status = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
            lease = conn.execute("SELECT 1 FROM execution_claims WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        self.assertEqual(opp_status, "approved_for_work")
        self.assertEqual(task_status, "needs_manual_review")
        self.assertIsNone(lease)

    def test_scheduler_rejects_unreasonable_poll_intervals(self):
        scheduler = AutonomousDaemonScheduler()
        for interval in (0, 9, 86401, True, 10.5):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    scheduler.run_forever(interval_seconds=interval)  # type: ignore[arg-type]

    def test_invoice_generator_rejects_path_traversal_invoice_number(self):
        with tempfile.TemporaryDirectory() as folder:
            malicious = {
                "invoice_number": "../../outside",
                "client_name": "Client",
                "amount_subtotal": 100.0,
                "tax_rate": 0,
                "tax_amount": 0,
                "amount_total": 100.0,
            }
            with self.assertRaises(ValueError):
                render_invoice_html(malicious, output_dir=Path(folder))

    def test_invoice_rejects_boolean_and_fractional_due_days(self):
        opportunity_id, _task_id = self._submitted_opportunity_with_task("Invoice numeric boundary")
        for invalid_due in (True, 1.5, float("inf")):
            with self.subTest(due_days=invalid_due):
                with self.assertRaises(ValueError):
                    create_invoice(opportunity_id, "Client", amount_subtotal=1000, due_days=invalid_due)

    def test_payment_verification_requires_boolean_confirmation_and_numeric_amount(self):
        opportunity_id, _task_id = self._submitted_opportunity_with_task("Payment input validation")
        invoice = create_invoice(opportunity_id, "Client", amount_subtotal=1000)
        with self.assertRaises(ValueError):
            verify_invoice_payment(invoice["id"], "UTR-TRUE-1", 1000, owner_confirmation="true")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            verify_invoice_payment(invoice["id"], "UTR-BOOL-2", True, owner_confirmation=True)

    def test_invoice_payment_reference_cannot_be_reused(self):
        opp1, _task1 = self._submitted_opportunity_with_task("First invoice")
        opp2, _task2 = self._submitted_opportunity_with_task("Second invoice")
        inv1 = create_invoice(opp1, "Client One", amount_subtotal=1000)
        inv2 = create_invoice(opp2, "Client Two", amount_subtotal=1000)
        verify_invoice_payment(inv1["id"], "UTR-SHARED-123", 1000, owner_confirmation=True)
        with self.assertRaises(ValueError):
            verify_invoice_payment(inv2["id"], "UTR-SHARED-123", 1000, owner_confirmation=True)

    def test_concurrent_payment_verification_cannot_double_use_reference(self):
        opp1, _task1 = self._submitted_opportunity_with_task("Concurrent invoice one")
        opp2, _task2 = self._submitted_opportunity_with_task("Concurrent invoice two")
        inv1 = create_invoice(opp1, "Client One", amount_subtotal=1250)
        inv2 = create_invoice(opp2, "Client Two", amount_subtotal=1250)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def verify(invoice_id: int) -> None:
            barrier.wait(timeout=5)
            try:
                verify_invoice_payment(invoice_id, "UTR-CONCURRENT-456", 1250, owner_confirmation=True)
                value = "paid"
            except ValueError as exc:
                value = f"rejected:{exc}"
            with lock:
                outcomes.append(value)

        threads = [
            threading.Thread(target=verify, args=(inv1["id"],)),
            threading.Thread(target=verify, args=(inv2["id"],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(outcomes.count("paid"), 1)
        with connect() as conn:
            paid_count = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE payment_reference=? AND status='paid'",
                ("UTR-CONCURRENT-456",),
            ).fetchone()[0]
        self.assertEqual(paid_count, 1)


if __name__ == "__main__":
    unittest.main()
