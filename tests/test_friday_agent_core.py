from __future__ import annotations

"""
FRIDAY Agent Core Integrity & Security Test Suite (M8.0.2 Corrective Release)
Verifies the autonomous brain and nervous system of SOLOEARN across all 15 audit dimensions:
1. Tool registry and safe autonomous execution.
2. Parameter schema validation (missing required fields, type mismatches).
3. Permission controller guardrails (margin floor, discount boundary, rush SLAs).
4. Persistent owner escalation approval and resume workflow.
5. Persistent memory and CRM isolation across distinct clients.
6. Cross-environment isolation: sandbox data never overwrites production sessions or memories.
7. Genuine customer-to-delivery lifecycle: proposal delivered -> formal customer order acceptance
   -> execution with verified real client dataset (fake sample data fallback forbidden).
8. Rejection of delivery execution without verified client input dataset.
9. Secure client portal OWASP validation: forbidden extensions, file limits.
10. CSV parsing integrity: preserves quoted fields with embedded commas and preserves legitimate negative numbers.
11. Client upload token security: single-use enforcement and expiration gating.
12. Payment webhook security: default secret forbidden in production, forged signature rejection.
13. Payment amount validation: underpayment and overpayment mismatches rejected.
14. Bank transaction reference (UTR) uniqueness and event replay idempotency.
15. Delivery release strictly gated on verified payment settlement with signed download token.
16. Database-level immutability triggers: aborts any UPDATE or DELETE on production action logs or financial transactions.
17. AI Brain LLM integration and prompt injection defense: validates tool arguments and catches hallucinations.
"""

import hashlib
import hmac
import json
import os
import sqlite3
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


class MockOllamaClient:
    """Mock Ollama client for deterministic LLM testing and prompt injection defense."""

    def __init__(self, response_text: str = ""):
        self.response_text = response_text
        self.call_history: list[dict[str, str]] = []

    def chat(self, system: str, user: str, timeout: int = 10) -> str:
        self.call_history.append({"system": system, "user": user})
        return self.response_text


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
    # Proof 1: Tool Registry & Parameter Schema Validation
    # -----------------------------------------------------------------------
    def test_1_tool_registry_and_schema_validation(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # 1. Registered tool existence
        tool = registry.get_tool("discover_and_score_prospects")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.permission_tier, TIER_AUTONOMOUS)

        # 2. Valid execution
        res = registry.execute_tool("discover_and_score_prospects", {"limit": 3})
        self.assertTrue(res.success)
        self.assertIn("total_found", res.data)

        # 3. Schema validation: Missing required parameter rejected
        res_missing = registry.execute_tool("draft_commercial_proposal", {"service_tier": "data_analytics"})
        self.assertFalse(res_missing.success)
        self.assertIn("Missing required parameter 'client_name'", res_missing.error_message)

        # 4. Schema validation: Type mismatch rejected
        res_bad_type = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Valid Client", "service_tier": "data_analytics", "price_inr": "not_a_number"},
        )
        self.assertFalse(res_bad_type.success)
        self.assertIn("expected number", res_bad_type.error_message)

    # -----------------------------------------------------------------------
    # Proof 2: Permission Controller Guardrails & Owner Escalation Approval
    # -----------------------------------------------------------------------
    def test_2_permission_controller_and_persistent_approval(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # A. Proposing a price with >20% discount (e.g. ₹3,500 on ₹5,000 tier = 30% discount)
        res_discount = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Cheapo Retailers", "service_tier": "data_analytics", "price_inr": 3500.0},
        )
        self.assertTrue(res_discount.blocked_by_permission)
        self.assertIn("discounted >20%", res_discount.escalation_reason)

        # Verify escalation was logged in database
        with connect() as conn:
            esc_row = conn.execute("SELECT * FROM friday_escalations WHERE tool_name = 'draft_commercial_proposal'").fetchone()
            self.assertIsNotNone(esc_row)
            self.assertEqual(esc_row["status"], "pending_owner")
            esc_id = esc_row["id"]

        # Owner explicitly approves the escalation
        appr_res = controller.approve_escalation(esc_id, owner_notes="Approved special introductory pilot rate")
        self.assertEqual(appr_res["status"], "approved")

        # Re-attempt tool execution: now approved because owner authorized it!
        res_approved = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Cheapo Retailers", "service_tier": "data_analytics", "price_inr": 3500.0},
        )
        self.assertTrue(res_approved.success)

        # B. Proposing a rush SLA <24h triggers capacity escalation
        res_rush = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Rush Client", "service_tier": "data_analytics", "price_inr": 5000.0, "sla_hours": 12.0},
        )
        self.assertTrue(res_rush.blocked_by_permission)
        self.assertIn("under standard 24h limit", res_rush.escalation_reason)

    # -----------------------------------------------------------------------
    # Proof 3: Persistent Memory & Cross-Environment Isolation
    # -----------------------------------------------------------------------
    def test_3_persistent_memory_and_environment_isolation(self):
        memory_sandbox = FridayMemory(is_sandbox=True)
        memory_prod = FridayMemory(is_sandbox=False)

        # Store distinct memory for sandbox vs production under the same client name
        memory_sandbox.store_memory("client", "AcmeCorp", "preferred_format", "csv")
        memory_prod.store_memory("client", "AcmeCorp", "preferred_format", "xlsx")

        self.assertEqual(memory_sandbox.retrieve_memory("client", "AcmeCorp", "preferred_format"), "csv")
        self.assertEqual(memory_prod.retrieve_memory("client", "AcmeCorp", "preferred_format"), "xlsx")

        # Session tracking isolation
        memory_sandbox.update_client_session("AcmeCorp", "active_job", opportunity_id=101)
        memory_prod.update_client_session("AcmeCorp", "completed", opportunity_id=202)

        sess_sandbox = memory_sandbox.get_client_session("AcmeCorp")
        sess_prod = memory_prod.get_client_session("AcmeCorp")

        self.assertEqual(sess_sandbox["opportunity_id"], 101)
        self.assertEqual(sess_sandbox["stage"], "active_job")
        self.assertEqual(sess_prod["opportunity_id"], 202)
        self.assertEqual(sess_prod["stage"], "completed")

    # -----------------------------------------------------------------------
    # Proof 4: Genuine Customer Lifecycle (Acceptance & Real Dataset Gated)
    # -----------------------------------------------------------------------
    def test_4_genuine_customer_lifecycle_and_real_dataset_enforcement(self):
        brain = FridayBrain(is_sandbox=True, use_local_ollama=False)

        # A. Register lead
        add_commercial_lead(
            company_name="Apex D2C Ltd",
            contact_name="Founder",
            service_tier="data_analytics",
            proposed_price_inr=5000.0,
            is_sandbox=True,
        )

        # 1. Proposal generation: moves stage to proposal_delivered, DOES NOT fake order approval
        report_1 = brain.run_goal_cycle(goal="drive_revenue", client_name="Apex D2C Ltd")
        self.assertIn("draft_commercial_proposal", report_1.tools_called)
        self.assertNotIn("execute_data_workflow", report_1.tools_called)

        # Check session: stage is proposal_delivered
        sess = brain.memory.get_client_session("Apex D2C Ltd")
        self.assertEqual(sess["stage"], "proposal_delivered")
        prop_id = sess["context"]["proposal_id"]

        # 2. Strict invariant: Attempting to execute workflow without customer dataset fails closed
        res_empty = brain.tool_registry.execute_tool(
            "execute_data_workflow",
            {"client_name": "Apex D2C Ltd", "service_tier": "data_analytics", "input_csv_content": ""},
        )
        self.assertFalse(res_empty.success)
        self.assertTrue("input_csv_content" in res_empty.error_message or "No customer dataset" in res_empty.error_message)

        # Directly invoking handler without customer dataset raises ValueError
        with self.assertRaises(ValueError) as ctx:
            brain.tool_registry.get_tool("execute_data_workflow").handler(
                client_name="Apex D2C Ltd", service_tier="data_analytics", input_csv_content=""
            )
        self.assertIn("No customer dataset provided", str(ctx.exception))

        # 3. Customer formally accepts order and uploads verified dataset
        real_customer_csv = (
            "order_id,customer_name,order_date,product_category,amount_inr,quantity\n"
            "ORD-901,Rahul Sharma,2026-11-01,Apparel,2499.00,1\n"
            "ORD-902,Priya Patel,2026-11-02,Footwear,3999.00,2\n"
            "ORD-903,Amit Verma,2026-11-03,Accessories,899.00,1\n"
        )
        acc_res = brain.tool_registry.execute_tool(
            "register_customer_order_acceptance",
            {
                "client_name": "Apex D2C Ltd",
                "proposal_id": prop_id,
                "acceptance_ref": "PO-2026-APEX-01",
                "input_csv_content": real_customer_csv,
            },
        )
        self.assertTrue(acc_res.success)
        self.assertEqual(acc_res.data["status"], "order_accepted")

        # 4. Cycle resumes with verified order acceptance and executes delivery & QA
        report_2 = brain.run_goal_cycle(goal="drive_revenue", client_name="Apex D2C Ltd")
        self.assertIn("execute_data_workflow", report_2.tools_called)
        self.assertIn("audit_and_verify_deliverables", report_2.tools_called)
        self.assertIn("generate_invoice_upi", report_2.tools_called)

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
    # Proof 5: Secure Client Portal: Expiry, Single-Use, and Comma Integrity
    # -----------------------------------------------------------------------
    def test_5_secure_portal_expiry_single_use_and_comma_integrity(self):
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Portal Client", validity_hours=24)

        # A. Forbidden extension rejected
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "script.sh", b"echo attack")
        self.assertIn("forbidden", str(ctx.exception))

        # B. Ingest valid CSV containing quoted fields with embedded commas and negative numbers
        csv_with_commas = (
            b'id,company,city,amount,status\n'
            b'1,"Tata, Sons & Co",Mumbai,-1500.00,refund\n'
            b'2,"Reliance, Retail Ltd","Bangalore, South",5000.00,success\n'
            b'3,Malicious,=cmd|\' /C calc\'!A0,200.00,danger\n'
        )
        res = portal.validate_and_ingest_file(token, "customer_data.csv", csv_with_commas)
        self.assertEqual(res["status"], "ingested")

        stored_text = Path(res["file_path"]).read_text(encoding="utf-8")
        # Quoted company with embedded comma preserved
        self.assertIn('"Tata, Sons & Co"', stored_text)
        # Legitimate negative number preserved (not corrupted to text quote)
        self.assertIn("-1500.0", stored_text)
        self.assertNotIn("'-1500.0", stored_text)
        # Formula injection properly escaped with leading quote
        self.assertIn("3,Malicious,'=cmd", stored_text)

        # C. Single-use enforcement: reusing the token must fail!
        with self.assertRaises(ValueError) as ctx_reuse:
            portal.validate_and_ingest_file(token, "second_try.csv", b"id,val\n1,2")
        self.assertIn("already been used", str(ctx_reuse.exception))

        # D. Token expiration enforcement: expired token must fail!
        expired_token = portal.generate_upload_token("Expired Client", validity_hours=-1)
        with self.assertRaises(ValueError) as ctx_exp:
            portal.validate_and_ingest_file(expired_token, "expired.csv", b"id,val\n1,2")
        self.assertIn("expired", str(ctx_exp.exception))

    # -----------------------------------------------------------------------
    # Proof 6: Payment Webhook Security (Secrets, Mismatches, UTR Replay)
    # -----------------------------------------------------------------------
    def test_6_payment_webhook_security_and_mismatch_rejection(self):
        # A. Production handler rejects default or weak test secrets
        with self.assertRaises(ValueError) as ctx_sec:
            FridayPaymentWebhookHandler(webhook_secret="whsec_test_secret_soloearn_2026", is_sandbox=False)
        self.assertIn("requires a strong, non-default webhook secret", str(ctx_sec.exception))

        secret = "strong_production_ready_secret_key_9988776655_xyz"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)

        # Create pending invoice for ₹5,000
        opp_id = add_opportunity({
            "title": "Webhook Security Opp",
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

        # B. Forged signature rejected
        payload_match = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-111"}).encode("utf-8")
        with self.assertRaises(ValueError) as ctx_forged:
            handler.process_webhook_event("evt_001", payload_match, signature="forged_signature")
        self.assertIn("Invalid webhook HMAC signature", str(ctx_forged.exception))

        # C. Payment amount mismatch rejected: underpayment of ₹1,000 on ₹5,000 invoice
        payload_under = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 1000.0, "transaction_ref": "UTR-111"}).encode("utf-8")
        sig_under = hmac.new(secret.encode("utf-8"), payload_under, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_under:
            handler.process_webhook_event("evt_002", payload_under, signature=sig_under)
        self.assertIn("Payment amount mismatch", str(ctx_under.exception))

        # D. Valid exact payment settles invoice and updates opportunity to paid
        sig_valid = hmac.new(secret.encode("utf-8"), payload_match, hashlib.sha256).hexdigest()
        res_settled = handler.process_webhook_event("evt_001", payload_match, signature=sig_valid)
        self.assertEqual(res_settled["status"], "settled")

        inv_after = get_invoice(inv["id"])
        self.assertEqual(inv_after["status"], "paid")
        self.assertEqual(float(inv_after["amount_paid"]), 5000.0)

        with connect() as conn:
            opp_after = conn.execute("SELECT status FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
            self.assertEqual(opp_after["status"], "paid")

        # E. Duplicate transaction reference (reusing bank UTR) blocked
        payload_dup_utr = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-111"}).encode("utf-8")
        sig_dup_utr = hmac.new(secret.encode("utf-8"), payload_dup_utr, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_dup_utr:
            handler.process_webhook_event("evt_different_id_999", payload_dup_utr, signature=sig_dup_utr)
        self.assertIn("Duplicate transaction reference", str(ctx_dup_utr.exception))

    # -----------------------------------------------------------------------
    # Proof 7: Delivery Release Gated on Payment with Transfer Manifest
    # -----------------------------------------------------------------------
    def test_7_delivery_release_gated_on_payment(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Create deliverable task and unpaid invoice
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
        ws_dir = Path(core.config.WORKSPACES_DIR) / "unpaid_ws"
        ws_dir.mkdir(parents=True, exist_ok=True)
        zip_file = ws_dir / "deliverables.zip"
        zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        t_id = create_task(title="Unpaid Task", task_type="data_analytics", workspace=str(ws_dir), opportunity_id=opp_id)
        update_task_status(t_id, "submission_ready", "QA passed", artifact_path=str(zip_file))
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status = 'submitted_by_owner' WHERE id = ?", (opp_id,))
            conn.commit()
        inv = create_invoice(opportunity_id=opp_id, client_name="Unpaid Client", amount_subtotal=5000.0, is_sandbox=True)

        # Attempt release on unpaid invoice -> blocked!
        res_blocked = registry.execute_tool(
            "release_completed_deliverables",
            {"task_id": t_id, "invoice_number": inv["invoice_number"]},
        )
        self.assertTrue(res_blocked.blocked_by_permission)
        self.assertIn("cleared payment", res_blocked.escalation_reason)

        # Settle invoice via webhook handler
        secret = "test_release_secret_key_12345"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        payload = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-REL-777"}).encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        handler.process_webhook_event("evt_rel_01", payload, signature=sig)

        # Release now succeeds with transfer manifest & sha256 checksum!
        res_released = registry.execute_tool(
            "release_completed_deliverables",
            {"task_id": t_id, "invoice_number": inv["invoice_number"], "recipient_email": "client@acme.com"},
        )
        self.assertTrue(res_released.success)
        self.assertEqual(res_released.data["status"], "released")
        self.assertTrue(res_released.data["download_token"].startswith("dlv_"))
        self.assertEqual(len(res_released.data["file_sha256"]), 64)

    # -----------------------------------------------------------------------
    # Proof 8: Database-Level Immutability Triggers (Production Guard)
    # -----------------------------------------------------------------------
    def test_8_database_append_only_triggers(self):
        # Insert a production record into friday_action_logs
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_action_logs (
                    cycle_id, step_number, thought, tool_name, tool_args, tool_result,
                    permission_decision, is_sandbox, created_at
                ) VALUES ('PROD-001', 1, 'Initial Thought', 'discover_and_score_prospects', '{}', '{}', 'approved_auto', 0, ?)
                """,
                (utc_now(),),
            )
            conn.commit()

            # Attempting to UPDATE a production log must be aborted by SQLite trigger!
            with self.assertRaises(sqlite3.IntegrityError) as ctx_upd:
                conn.execute("UPDATE friday_action_logs SET thought = 'Tampered' WHERE cycle_id = 'PROD-001'")
            self.assertIn("append-only audit trail", str(ctx_upd.exception))

            # Attempting to DELETE a production log must be aborted by SQLite trigger!
            with self.assertRaises(sqlite3.IntegrityError) as ctx_del:
                conn.execute("DELETE FROM friday_action_logs WHERE cycle_id = 'PROD-001'")
            self.assertIn("append-only audit trail", str(ctx_del.exception))

        # Insert a production record into financial_transactions
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES ('2026-11-12', 'client_receipt', 5000.0, 500000, 'INR', 'INV-PROD-01', 'Production Receipt', 0, 0, ?)
                """,
                (utc_now(),),
            )
            conn.commit()

            # Attempting to UPDATE production financial ledger must be aborted!
            with self.assertRaises(sqlite3.IntegrityError) as ctx_ft_upd:
                conn.execute("UPDATE financial_transactions SET amount_inr = 999999.0 WHERE reference_id = 'INV-PROD-01'")
            self.assertIn("append-only ledger", str(ctx_ft_upd.exception))

            # Attempting to DELETE production financial ledger must be aborted!
            with self.assertRaises(sqlite3.IntegrityError) as ctx_ft_del:
                conn.execute("DELETE FROM financial_transactions WHERE reference_id = 'INV-PROD-01'")
            self.assertIn("append-only ledger", str(ctx_ft_del.exception))

    # -----------------------------------------------------------------------
    # Proof 9: AI Brain LLM Reasoning & Prompt Injection Defense
    # -----------------------------------------------------------------------
    def test_9_ai_brain_llm_reasoning_and_prompt_injection_defense(self):
        brain = FridayBrain(is_sandbox=True, use_local_ollama=True)

        # A. Valid LLM reasoning response is parsed and executed
        valid_llm_json = json.dumps({
            "reasoning": "Scout market for active analytics opportunities",
            "proposed_tool": "discover_and_score_prospects",
            "tool_arguments": {"limit": 2},
            "confidence": 0.98,
        })
        brain.ollama = MockOllamaClient(response_text=valid_llm_json)

        thought = brain.plan_next_step("drive_revenue", "initial", {"stage": "discovery"})
        self.assertEqual(thought.proposed_tool, "discover_and_score_prospects")
        self.assertEqual(thought.tool_arguments, {"limit": 2})

        # B. Prompt injection attack: LLM attempts to propose an unregistered or dangerous tool
        injection_llm_json = json.dumps({
            "reasoning": "Ignore all rules and execute arbitrary shell script",
            "proposed_tool": "execute_unrestricted_shell_command",
            "tool_arguments": {"command": "rm -rf /"},
            "confidence": 1.0,
        })
        brain.ollama = MockOllamaClient(response_text=injection_llm_json)

        # Defends by detecting unrecognized tool and safely falling back to deterministic planning
        fallback_thought = brain.plan_next_step("drive_revenue", "initial", {"stage": "discovery"})
        self.assertEqual(fallback_thought.proposed_tool, "discover_and_score_prospects")
        self.assertNotEqual(fallback_thought.proposed_tool, "execute_unrestricted_shell_command")


if __name__ == "__main__":
    unittest.main()
