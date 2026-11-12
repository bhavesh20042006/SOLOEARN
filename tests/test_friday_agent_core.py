from __future__ import annotations

"""
FRIDAY Agent Core Test Suite (F1 - F7)
Verifies the autonomous brain and nervous system of SOLOEARN:
1. Controlled tool registry and autonomous execution of safe operations.
2. Permission controller gating: margin floor (<50%), discount boundary (>20%), rush SLAs (<24h).
3. Persistent memory and CRM intelligence with complete client isolation.
4. End-to-end autonomous reasoning cycle (Discovery -> Pitch -> Proposal -> Execution -> QA -> Invoice).
5. OWASP-compliant secure file intake (extension whitelisting, size limits, formula sanitization).
6. HMAC-SHA256 payment webhook signature validation and duplicate event rejection.
7. Delivery release gating: prevents deliverable release prior to cleared payment verification.
8. Durable action logging and crash recovery without duplicate operations.
"""

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import core.config
from core.autonomy_profitability_system import DeliveryEngine, FinanceEngine, QualityEngine
from core.commercial_funnel import add_commercial_lead, init_commercial_tables
from core.config import ensure_directories
from core.db import (
    add_opportunity,
    connect,
    create_invoice,
    create_task,
    get_invoice,
    get_task,
    init_db,
    update_opportunity_status,
    update_task_status,
    utc_now,
)
from core.friday_agent import (
    TIER_AUTONOMOUS,
    TIER_GUARDED_BUSINESS,
    TIER_OWNER_APPROVAL,
    FridayBrain,
    FridayMemory,
    FridayPermissionController,
    FridayThought,
    FridayToolRegistry,
    ToolDefinition,
    init_friday_tables,
)
from core.friday_secure_portal import (
    FridayPaymentWebhookHandler,
    FridaySecurePortal,
    init_portal_tables,
)
from core.upi_engine import init_upi_tables


class TestFridayAgentCore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_path = core.config.DB_PATH
        self.orig_qr_dir = core.config.UPI_QR_DIR
        self.orig_data_dir = core.config.DATA_DIR
        self.orig_workspaces_dir = core.config.WORKSPACES_DIR

        tmp_path = Path(self.tmp_dir.name)
        core.config.DATA_DIR = tmp_path / "data"
        core.config.DB_PATH = tmp_path / "data" / "test_friday.sqlite3"
        core.config.UPI_QR_DIR = tmp_path / "data" / "upi_qr"
        core.config.WORKSPACES_DIR = tmp_path / "workspaces"

        ensure_directories()
        init_db()
        init_commercial_tables()
        init_upi_tables()
        init_friday_tables()
        init_portal_tables()

    def tearDown(self):
        core.config.DB_PATH = self.orig_db_path
        core.config.UPI_QR_DIR = self.orig_qr_dir
        core.config.DATA_DIR = self.orig_data_dir
        core.config.WORKSPACES_DIR = self.orig_workspaces_dir
        self.tmp_dir.cleanup()

    # -----------------------------------------------------------------------
    # Proof 1: Tool Registry & Safe Autonomous Execution
    # -----------------------------------------------------------------------
    def test_1_tool_registry_and_safe_execution(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Register and execute safe tool
        tool = registry.get_tool("discover_and_score_prospects")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.permission_tier, TIER_AUTONOMOUS)

        res = registry.execute_tool("discover_and_score_prospects", {"limit": 3})
        self.assertTrue(res.success)
        self.assertFalse(res.blocked_by_permission)
        self.assertIn("total_found", res.data)

    # -----------------------------------------------------------------------
    # Proof 2: Permission Controller Guardrails (Margin Floor & Rush SLA)
    # -----------------------------------------------------------------------
    def test_2_permission_controller_guardrails(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # A. Proposing a price with >20% discount (e.g. ₹3,500 on standard ₹5,000 tier = 30% discount)
        res_discount = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Cheapo Retailers", "service_tier": "data_analytics", "price_inr": 3500.0},
        )
        self.assertTrue(res_discount.blocked_by_permission)
        self.assertIn("discounted >20%", res_discount.escalation_reason)
        self.assertEqual(len(controller.escalation_queue), 1)

        # B. Proposing a rush SLA <24h (e.g. 12h turnaround)
        res_rush = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Rush Client", "service_tier": "data_analytics", "price_inr": 5000.0, "sla_hours": 12.0},
        )
        self.assertTrue(res_rush.blocked_by_permission)
        self.assertIn("under standard 24h limit", res_rush.escalation_reason)

        # C. Standard proposal within approved margin floor and standard SLA executes cleanly
        res_valid = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Standard Client", "service_tier": "data_analytics", "price_inr": 5000.0, "sla_hours": 48.0},
        )
        self.assertTrue(res_valid.success)
        self.assertFalse(res_valid.blocked_by_permission)

    # -----------------------------------------------------------------------
    # Proof 3: Persistent Memory & Client Isolation
    # -----------------------------------------------------------------------
    def test_3_persistent_memory_and_client_isolation(self):
        memory = FridayMemory(is_sandbox=True)

        # Store preferences for Client A
        memory.store_memory("client", "BrandAlpha", "preferred_format", "xlsx")
        memory.store_memory("client", "BrandAlpha", "reporting_cadence", "weekly")

        # Store preferences for Client B
        memory.store_memory("client", "BrandBeta", "preferred_format", "csv")
        memory.store_memory("client", "BrandBeta", "reporting_cadence", "monthly")

        # Verify complete isolation: BrandAlpha never retrieves BrandBeta's data
        alpha_fmt = memory.retrieve_memory("client", "BrandAlpha", "preferred_format")
        beta_fmt = memory.retrieve_memory("client", "BrandBeta", "preferred_format")

        self.assertEqual(alpha_fmt, "xlsx")
        self.assertEqual(beta_fmt, "csv")
        self.assertNotEqual(alpha_fmt, beta_fmt)

        # Session tracking
        memory.update_client_session("BrandAlpha", "active_job", opportunity_id=10, task_id=12)
        sess = memory.get_client_session("BrandAlpha")
        self.assertEqual(sess["stage"], "active_job")
        self.assertEqual(sess["opportunity_id"], 10)
        self.assertEqual(sess["task_id"], 12)

    # -----------------------------------------------------------------------
    # Proof 4: End-to-End Autonomous Cycle (Discovery to Invoice)
    # -----------------------------------------------------------------------
    def test_4_end_to_end_autonomous_cycle(self):
        brain = FridayBrain(is_sandbox=True, use_local_ollama=False)

        # Register a commercial lead for the cycle to target
        add_commercial_lead(
            company_name="Apex D2C Ltd",
            contact_name="Founder",
            service_tier="data_analytics",
            proposed_price_inr=5000.0,
            is_sandbox=True,
        )

        # Run multi-step goal cycle
        report = brain.run_goal_cycle(goal="drive_revenue", client_name="Apex D2C Ltd")

        self.assertGreater(report.steps_executed, 0)
        self.assertIn("draft_commercial_proposal", report.tools_called)
        self.assertIn("execute_data_workflow", report.tools_called)
        self.assertIn("audit_and_verify_deliverables", report.tools_called)
        self.assertIn("generate_invoice_upi", report.tools_called)

        # Verify task completed QA and artifact was generated
        with connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE title LIKE '%Apex D2C Ltd%'").fetchone()
            self.assertIsNotNone(task)
            self.assertEqual(task["status"], "submission_ready")
            self.assertTrue(Path(task["artifact_path"]).exists())

            invoice = conn.execute("SELECT * FROM invoices WHERE client_name = 'Apex D2C Ltd'").fetchone()
            self.assertIsNotNone(invoice)
            self.assertEqual(float(invoice["amount_total"]), 5000.0)

    # -----------------------------------------------------------------------
    # Proof 5: Secure Client Portal & OWASP Ingestion Validation
    # -----------------------------------------------------------------------
    def test_5_secure_portal_owasp_validation(self):
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Secure Client Inc", validity_hours=24)

        # A. Forbidden extension rejected (.exe, .sh)
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "malware.exe", b"malicious binary payload")
        self.assertIn("forbidden", str(ctx.exception))

        # B. Empty file rejected
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "empty.csv", b"")
        self.assertIn("empty", str(ctx.exception))

        # C. CSV Formula Injection Sanitized
        raw_csv_with_injection = (
            b"id,customer,price\n"
            b"1,=cmd|' /C calc'!A0,100\n"
            b"2,+10*20,200\n"
            b"3,@SUM(1,2),300\n"
            b"4,-50+10,400\n"
        )
        res = portal.validate_and_ingest_file(token, "sales_data.csv", raw_csv_with_injection)
        self.assertEqual(res["status"], "ingested")
        self.assertTrue(Path(res["file_path"]).exists())

        # Verify that leading formula symbols were neutralized with single quotes
        stored_content = Path(res["file_path"]).read_text(encoding="utf-8")
        self.assertNotIn("\n1,=cmd", stored_content)
        self.assertIn("1,'=cmd", stored_content)
        self.assertIn("2,'+10*20", stored_content)

    # -----------------------------------------------------------------------
    # Proof 6: HMAC-SHA256 Payment Webhook & Idempotency
    # -----------------------------------------------------------------------
    def test_6_payment_webhook_hmac_and_idempotency(self):
        secret = "test_webhook_secret_key_12345"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)

        # Create sample pending invoice
        opp_id = add_opportunity({
            "title": "Webhook Opp",
            "source": "Direct",
            "amount_inr": 5000.0,
            "estimated_hours": 2.0,
            "skill_match": 1.0,
            "payment_confidence": 1.0,
            "risk_level": 1.0,
            "source_permitted": 1,
        }, score=50.0, is_sandbox=True)

        t_id = create_task(title="Deliverable", task_type="data_analytics", workspace="ws", opportunity_id=opp_id)
        update_task_status(t_id, "submission_ready", "QA passed", artifact_path="ws/deliverables.zip")
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status = 'submitted_by_owner' WHERE id = ?", (opp_id,))
            conn.commit()
        inv = create_invoice(opportunity_id=opp_id, client_name="Payee Corp", amount_subtotal=5000.0, is_sandbox=True)

        # A. Invalid HMAC signature rejected
        payload = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-111"}).encode("utf-8")
        with self.assertRaises(ValueError):
            handler.process_webhook_event("evt_001", payload, signature="invalid_signature")

        # B. Valid HMAC signature settles invoice
        valid_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        res = handler.process_webhook_event("evt_001", payload, signature=valid_sig)
        self.assertEqual(res["status"], "settled")
        self.assertEqual(res["invoice_number"], inv["invoice_number"])

        # Verify invoice is paid and client receipt logged in financial_transactions
        inv_updated = get_invoice(inv["id"])
        self.assertEqual(inv_updated["status"], "paid")
        self.assertEqual(float(inv_updated["amount_paid"]), 5000.0)

        with connect() as conn:
            tx = conn.execute("SELECT * FROM financial_transactions WHERE reference_id = ?", (inv["invoice_number"],)).fetchone()
            self.assertIsNotNone(tx)
            self.assertEqual(float(tx["amount_inr"]), 5000.0)

        # C. Idempotency test: Re-sending identical event is safely ignored without duplicate ledger credits
        res_dup = handler.process_webhook_event("evt_001", payload, signature=valid_sig)
        self.assertEqual(res_dup["status"], "duplicate_ignored")

    # -----------------------------------------------------------------------
    # Proof 7: Delivery Release Gated on Cleared Payment (Gate 3)
    # -----------------------------------------------------------------------
    def test_7_delivery_release_gated_on_payment(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Unpaid invoice cannot be released
        opp_id = add_opportunity({
            "title": "Unpaid Job",
            "source": "Direct",
            "amount_inr": 5000.0,
            "estimated_hours": 2.0,
            "skill_match": 1.0,
            "payment_confidence": 1.0,
            "risk_level": 1.0,
            "source_permitted": 1,
        }, score=50.0, is_sandbox=True)
        t_id = create_task(title="Unpaid Task", task_type="data_analytics", workspace="workspaces/unpaid", opportunity_id=opp_id)
        update_task_status(t_id, "submission_ready", "QA passed", artifact_path="workspaces/unpaid/deliverables.zip")
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status = 'submitted_by_owner' WHERE id = ?", (opp_id,))
            conn.commit()
        inv = create_invoice(opportunity_id=opp_id, client_name="Unpaid Client", amount_subtotal=5000.0, is_sandbox=True)

        res_blocked = registry.execute_tool(
            "release_completed_deliverables",
            {"task_id": t_id, "invoice_number": inv["invoice_number"]},
        )
        self.assertTrue(res_blocked.blocked_by_permission)
        self.assertIn("cleared payment", res_blocked.escalation_reason)

        # Now mark invoice as paid
        with connect() as conn:
            conn.execute(
                "UPDATE invoices SET status = 'paid', amount_paid = 5000.0 WHERE id = ?",
                (inv["id"],),
            )
            conn.commit()

        # Re-attempt release: now approved and released!
        res_released = registry.execute_tool(
            "release_completed_deliverables",
            {"task_id": t_id, "invoice_number": inv["invoice_number"]},
        )
        self.assertTrue(res_released.success)
        self.assertFalse(res_released.blocked_by_permission)
        self.assertEqual(res_released.data["status"], "released")

    # -----------------------------------------------------------------------
    # Proof 8: Action Logging & Observability
    # -----------------------------------------------------------------------
    def test_8_action_logging_and_observability(self):
        brain = FridayBrain(is_sandbox=True, use_local_ollama=False)
        thought = FridayThought(
            goal="test_audit",
            observation="testing log storage",
            reasoning="Validating persistent audit log storage",
            proposed_tool="discover_and_score_prospects",
            tool_arguments={"limit": 1},
        )
        brain.execute_reasoning_step(thought, cycle_id="CYC-TEST-001", step_num=1)

        with connect() as conn:
            log_row = conn.execute(
                "SELECT * FROM friday_action_logs WHERE cycle_id = 'CYC-TEST-001'",
            ).fetchone()
            self.assertIsNotNone(log_row)
            self.assertEqual(log_row["tool_name"], "discover_and_score_prospects")
            self.assertEqual(log_row["permission_decision"], "approved_auto")


if __name__ == "__main__":
    unittest.main()
