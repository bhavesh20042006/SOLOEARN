from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from core.config import ensure_directories, safe_workspace_path
from core.db import (
    add_opportunity,
    claim_opportunity_for_execution,
    complete_revision,
    connect,
    create_invoice,
    create_revision_request,
    create_task,
    get_invoice,
    init_db,
    list_invoices,
    list_revisions,
    recover_interrupted_tasks,
    update_opportunity_status,
    verify_invoice_payment,
)
from core.discovery_engine import (
    parse_feed_xml,
    validate_source_url,
)
from core.execution_engine import AutonomousExecutionEngine
from core.invoice_generator import render_invoice_html
from core.qa_agent import IndependentQAAgent
from core.revenue_optimizer import RevenueOptimizer
from core.scheduler import AutonomousDaemonScheduler


class Milestone5RevenueOpsTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM revisions")
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM discovery_sources")
            conn.execute("DELETE FROM audit_log")

    # --- Security Hardening: SSRF, DNS & XXE ---

    def test_ssrf_rejects_localhost_and_private_ips(self):
        # Local loopbacks
        valid, _ = validate_source_url("http://127.0.0.1:8000/feed.xml")
        self.assertFalse(valid)
        valid, _ = validate_source_url("http://localhost/jobs.json")
        self.assertFalse(valid)
        # Cloud metadata service & private subnets
        valid, _ = validate_source_url("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(valid)
        valid, _ = validate_source_url("http://192.168.1.50/leads.xml")
        self.assertFalse(valid)
        valid, _ = validate_source_url("http://10.0.0.1/leads.json")
        self.assertFalse(valid)

    def test_xml_xxe_and_entity_expansion_blocked(self):
        # Malicious DOCTYPE / ENTITY injection
        evil_xml = """<?xml version="1.0"?>
        <!DOCTYPE foo [
          <!ENTITY xxe SYSTEM "file:///c:/windows/win.ini">
        ]>
        <rss version="2.0">
          <channel><title>&xxe;</title></channel>
        </rss>
        """
        with self.assertRaises(ValueError) as ctx:
            parse_feed_xml(evil_xml)
        self.assertTrue("entity declarations are not permitted" in str(ctx.exception) or "XXE" in str(ctx.exception))

    # --- Concurrency & Atomic Database Locking ---

    def test_atomic_execution_lock_prevents_race_conditions(self):
        opp_id = add_opportunity(
            {
                "title": "Concurrent Job Test",
                "source": "Direct",
                "url": "https://example.com/gig",
                "description": "Clean sales data and generate charts.",
                "amount_inr": 5000.0,
                "estimated_hours": 2.0,
                "skill_match": 0.9,
                "payment_confidence": 0.9,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=85.0,
        )
        update_opportunity_status(opp_id, "approved_for_work")

        # Two simultaneous claim attempts
        claim1 = claim_opportunity_for_execution(opp_id)
        claim2 = claim_opportunity_for_execution(opp_id)

        self.assertTrue(claim1)
        self.assertFalse(claim2)  # Second concurrent attempt must be rejected

    def test_multi_threaded_concurrency_guard(self):
        opp_id = add_opportunity(
            {
                "title": "Threaded Concurrency Test",
                "source": "Direct",
                "url": "https://example.com/gig2",
                "description": "Perform data analysis.",
                "amount_inr": 6000.0,
                "estimated_hours": 2.5,
                "skill_match": 0.85,
                "payment_confidence": 0.9,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=82.0,
        )
        update_opportunity_status(opp_id, "approved_for_work")

        claims = []
        threads = []

        def worker():
            res = claim_opportunity_for_execution(opp_id)
            claims.append(res)

        for _ in range(5):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Exactly 1 thread must succeed, and 4 must fail
        self.assertEqual(claims.count(True), 1)
        self.assertEqual(claims.count(False), 4)

    # --- Multi-Service Execution & Independent QA ---

    def test_data_engineering_service_execution_and_qa(self):
        opp_id = add_opportunity(
            {
                "title": "Build Automated ETL Data Pipeline",
                "source": "Inbound Lead",
                "url": "https://example.com/etl",
                "description": "Construct automated pipeline to clean raw records and produce schema manifest.",
                "amount_inr": 15000.0,
                "estimated_hours": 6.0,
                "skill_match": 0.9,
                "payment_confidence": 0.95,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=90.0,
        )
        update_opportunity_status(opp_id, "approved_for_work")

        csv_content = b"id,name,value\n1,Alpha,100\n2,Beta,200\n1,Alpha,100\n"
        engine = AutonomousExecutionEngine()
        task_id, workspace, plan = engine.execute_job(
            opportunity={"id": opp_id, "title": "Build Automated ETL Data Pipeline", "service_tier": "data_engineering", "source_permitted": True, "status": "approved_for_work"},
            file_bytes=csv_content,
            filename="raw_etl_input.csv",
        )

        self.assertTrue((workspace / "pipeline.py").exists())
        self.assertTrue((workspace / "cleaned_dataset.csv").exists())
        self.assertTrue((workspace / "schema_manifest.json").exists())
        self.assertTrue((workspace / "data_dictionary.md").exists())
        self.assertTrue((workspace / "deliverables.zip").exists())

        # Run independent QA on data engineering deliverable
        qa = IndependentQAAgent(workspace, task_id)
        report = qa.run_all_checks()
        self.assertTrue(report.passed)
        self.assertEqual(report.score_pct, 100.0)

    def test_interactive_dashboard_service_execution_and_qa(self):
        opp_id = add_opportunity(
            {
                "title": "Executive Sales Dashboard",
                "source": "Enterprise Client",
                "url": "https://example.com/dash",
                "description": "Develop standalone interactive dashboard with search filters and KPI metrics.",
                "amount_inr": 20000.0,
                "estimated_hours": 8.0,
                "skill_match": 0.95,
                "payment_confidence": 0.95,
                "risk_level": 1.5,
                "source_permitted": True,
            },
            score=95.0,
        )
        update_opportunity_status(opp_id, "approved_for_work")

        csv_content = b"month,revenue,units\nJan,15000,150\nFeb,22000,210\nMar,28000,290\n"
        engine = AutonomousExecutionEngine()
        task_id, workspace, plan = engine.execute_job(
            opportunity={"id": opp_id, "title": "Executive Sales Dashboard", "service_tier": "interactive_dashboard", "source_permitted": True, "status": "approved_for_work"},
            file_bytes=csv_content,
            filename="sales_monthly.csv",
        )

        self.assertTrue((workspace / "dashboard.html").exists())
        self.assertTrue((workspace / "kpi_metrics.json").exists())
        self.assertTrue((workspace / "dashboard_user_guide.md").exists())
        self.assertTrue((workspace / "deliverables.zip").exists())

        # Run independent QA on dashboard deliverable
        qa = IndependentQAAgent(workspace, task_id)
        report = qa.run_all_checks()
        self.assertTrue(report.passed)
        self.assertEqual(report.score_pct, 100.0)

    def test_business_automation_service_execution_and_qa(self):
        opp_id = add_opportunity(
            {
                "title": "Reconciliation Process Automation Script",
                "source": "Corporate Referral",
                "url": "https://example.com/auto",
                "description": "Automate file ingestion and batch reconciliation with JSON configuration.",
                "amount_inr": 15000.0,
                "estimated_hours": 5.0,
                "skill_match": 0.9,
                "payment_confidence": 0.9,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=88.0,
        )
        update_opportunity_status(opp_id, "approved_for_work")

        engine = AutonomousExecutionEngine()
        task_id, workspace, plan = engine.execute_job(
            opportunity={"id": opp_id, "title": "Reconciliation Process Automation Script", "service_tier": "business_automation", "source_permitted": True, "status": "approved_for_work"},
            file_bytes=b"dummy",
            filename="spec.txt",
        )

        self.assertTrue((workspace / "automation.py").exists())
        self.assertTrue((workspace / "config.json").exists())
        self.assertTrue((workspace / "README.md").exists())

        qa = IndependentQAAgent(workspace, task_id)
        report = qa.run_all_checks()
        self.assertTrue(report.passed)
        self.assertEqual(report.score_pct, 100.0)

    # --- Financial Invoicing & Verified Payment Workflow ---

    def test_invoice_creation_and_payment_verification_lifecycle(self):
        opp_id = add_opportunity(
            {
                "title": "Financial Audit & Data Prep",
                "source": "Direct Client",
                "url": "https://example.com/fin",
                "description": "Clean financial ledger.",
                "amount_inr": 25000.0,
                "estimated_hours": 10.0,
                "skill_match": 0.9,
                "payment_confidence": 0.95,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=90.0,
        )
        task_id = create_task("Data Prep", "data_prep", "workspace", opp_id)

        # Work must be marked submission_ready or submitted_by_owner before invoice
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task_id,))
        update_opportunity_status(opp_id, "approved_for_work")
        update_opportunity_status(opp_id, "in_progress")
        update_opportunity_status(opp_id, "submission_ready")
        update_opportunity_status(opp_id, "submitted_by_owner")

        # 1. Create Invoice
        invoice = create_invoice(
            opportunity_id=opp_id,
            client_name="Acme Corp",
            service_tier="data_engineering",
            amount_subtotal=25000.0,
            tax_rate=18.0,
            task_id=task_id,
            notes="Completed with 100% QA pass.",
        )
        self.assertEqual(invoice["amount_subtotal"], 25000.0)
        self.assertEqual(invoice["tax_amount"], 4500.0)
        self.assertEqual(invoice["amount_total"], 29500.0)
        self.assertEqual(invoice["status"], "issued")
        self.assertTrue(invoice["invoice_number"].startswith("INV-"))

        # Render HTML invoice
        html_file = render_invoice_html(invoice)
        self.assertTrue(html_file.exists())
        self.assertIn("Acme Corp", html_file.read_text(encoding="utf-8"))

        # 2. Rejection of unconfirmed payment verification
        with self.assertRaises(ValueError):
            verify_invoice_payment(
                invoice_id=invoice["id"],
                payment_reference="UTR12345678",
                verified_amount=29500.0,
                owner_confirmation=False,  # Owner MUST confirm
            )

        # 3. Rejection of payment amount mismatch
        with self.assertRaises(ValueError):
            verify_invoice_payment(
                invoice_id=invoice["id"],
                payment_reference="UTR12345678",
                verified_amount=20000.0,  # Incorrect amount
                owner_confirmation=True,
            )

        # 4. Successful verified payment confirmation
        paid_inv = verify_invoice_payment(
            invoice_id=invoice["id"],
            payment_reference="HDFC-UTR-9876543210",
            verified_amount=29500.0,
            owner_confirmation=True,
        )
        self.assertEqual(paid_inv["status"], "paid")
        self.assertEqual(paid_inv["payment_verified_by_owner"], 1)

        # Verify revenue ledger automatically updated
        with connect() as conn:
            rev_row = conn.execute("SELECT * FROM revenue WHERE opportunity_id=? AND status='paid'", (opp_id,)).fetchone()
            self.assertIsNotNone(rev_row)
            self.assertEqual(rev_row["amount_inr"], 29500.0)

    # --- Client Revisions Workflow ---

    def test_client_revision_workflow(self):
        opp_id = add_opportunity(
            {
                "title": "Client Revision Test",
                "source": "Direct",
                "url": "https://example.com/rev",
                "description": "Analysis needing revision.",
                "amount_inr": 8000.0,
                "estimated_hours": 3.0,
                "skill_match": 0.8,
                "payment_confidence": 0.9,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=80.0,
        )
        task_id = create_task("Task for revision", "analysis", "workspace", opp_id)

        rev_id = create_revision_request(task_id, "Please add categorical breakdown by region.")
        self.assertGreater(rev_id, 0)

        revs = list_revisions(task_id)
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0]["status"], "requested")

        complete_revision(rev_id, "Added regional breakdown in Sheet 2.")
        revs_after = list_revisions(task_id)
        self.assertEqual(revs_after[0]["status"], "completed")

    # --- Crash Recovery & Unattended Scheduler ---

    def test_scheduler_recovers_interrupted_tasks_and_writes_heartbeat(self):
        opp_id = add_opportunity(
            {
                "title": "Crash Sim Job",
                "source": "Direct",
                "url": "https://example.com/crash",
                "description": "Interrupted execution.",
                "amount_inr": 5000.0,
                "estimated_hours": 2.0,
                "skill_match": 0.9,
                "payment_confidence": 0.9,
                "risk_level": 2.0,
                "source_permitted": True,
            },
            score=85.0,
        )
        task_id = create_task("Crash task", "analysis", "workspace", opp_id)
        # Simulate abrupt interruption during execution
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status='in_progress' WHERE id=?", (opp_id,))
            conn.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (task_id,))

        with tempfile.TemporaryDirectory() as tmp_dir:
            hb_path = Path(tmp_dir) / "test_heartbeat.json"
            scheduler = AutonomousDaemonScheduler(heartbeat_path=hb_path)
            heartbeat = scheduler.run_once()

            self.assertEqual(heartbeat.status, "healthy")
            self.assertGreaterEqual(heartbeat.interrupted_tasks_recovered, 1)
            self.assertTrue(hb_path.exists())

            # Verify opportunity was safely reset
            with connect() as conn:
                opp_row = conn.execute("SELECT status FROM opportunities WHERE id=?", (opp_id,)).fetchone()
                self.assertEqual(opp_row["status"], "approved_for_work")

    # --- Revenue Optimizer (Minimum ₹1 Lakh/Month Target) ---

    def test_revenue_optimizer_target_calculations(self):
        plan = RevenueOptimizer.analyze_monthly_target(100000.0)
        self.assertEqual(plan.monthly_target_inr, 100000.0)
        self.assertGreaterEqual(plan.target_gap_inr, 0.0)
        self.assertIn("tier1_analytics", plan.recommended_jobs_needed)
        self.assertIn("tier2_engineering", plan.recommended_jobs_needed)
        self.assertGreater(len(plan.optimal_monthly_mix), 0)

        # Test yield ranking prioritizes low-risk, high-return opportunities
        opps = [
            {"title": "Low Value Low Rate", "amount_inr": 1000, "estimated_hours": 5, "payment_confidence": 0.5, "skill_match": 0.5, "risk_level": 8.0},
            {"title": "High Yield Premium", "amount_inr": 20000, "estimated_hours": 4, "payment_confidence": 0.95, "skill_match": 0.95, "risk_level": 1.0},
        ]
        ranked = RevenueOptimizer.rank_opportunities_by_hourly_yield(opps)
        self.assertEqual(ranked[0]["title"], "High Yield Premium")


if __name__ == "__main__":
    unittest.main()
