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

import concurrent.futures
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
    download_client_deliverable,
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

    def _create_test_invoice(
        self,
        client_name: str = "Test Client",
        amount: float = 5000.0,
        is_sandbox: bool = True,
        task_id: int | None = None,
        create_artifact: bool = True,
    ) -> tuple[int, int, dict[str, Any]]:
        opp_id = add_opportunity({
            "title": f"Opportunity for {client_name}",
            "source": "Direct",
            "amount_inr": amount,
            "estimated_hours": 2.0,
            "skill_match": 1.0,
            "payment_confidence": 1.0,
            "risk_level": 1.0,
            "source_permitted": 1,
        }, score=50.0, is_sandbox=is_sandbox)

        ws = Path(core.config.WORKSPACES_DIR) / f"ws_{opp_id}_{int(time.time()*1000)%10000}"
        ws.mkdir(parents=True, exist_ok=True)
        artifact_path = str(ws / "deliverables.zip")
        if create_artifact:
            Path(artifact_path).write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        t_id = task_id or create_task(title=f"Task for {client_name}", task_type="data_analytics", workspace=str(ws), opportunity_id=opp_id)
        update_task_status(t_id, "submission_ready", "QA passed", artifact_path=artifact_path)
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status = 'submitted_by_owner' WHERE id = ?", (opp_id,))
            conn.commit()

        inv = create_invoice(
            opportunity_id=opp_id,
            client_name=client_name,
            amount_subtotal=amount,
            task_id=t_id,
            is_sandbox=is_sandbox,
        )
        return opp_id, t_id, inv

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

    # -----------------------------------------------------------------------
    # Regression Test 1 (Audit Item 1): Acceptance with proposal belonging to another client
    # -----------------------------------------------------------------------
    def test_10_acceptance_with_proposal_belonging_to_another_client(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Create proposal for Client Alpha
        res_prop = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Client Alpha", "service_tier": "data_analytics", "price_inr": 5000.0},
        )
        self.assertTrue(res_prop.success)
        alpha_prop_id = res_prop.data["proposal_id"]

        # Attempt acceptance by Client Beta with Alpha's proposal_id
        with self.assertRaises(ValueError) as ctx:
            registry.get_tool("register_customer_order_acceptance").handler(
                client_name="Client Beta",
                proposal_id=alpha_prop_id,
                acceptance_ref="PO-BETA-999",
                input_csv_content="col1,col2\nval1,val2",
            )
        self.assertIn("does not belong to client 'Client Beta'", str(ctx.exception))

    # -----------------------------------------------------------------------
    # Regression Test 2 (Audit Item 2): Direct delivery execution without accepted order
    # -----------------------------------------------------------------------
    def test_11_direct_delivery_execution_without_accepted_order(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Attempt to execute workflow for client with no accepted order
        res = registry.execute_tool(
            "execute_data_workflow",
            {
                "client_name": "Unaccepted Corp",
                "service_tier": "data_analytics",
                "input_csv_content": "id,name\n1,Alpha",
            },
        )
        self.assertFalse(res.success)
        self.assertIn("No accepted customer order found", res.error_message)

    # -----------------------------------------------------------------------
    # Regression Test 3 (Audit Item 3): Direct delivery execution with fabricated input (SHA256 mismatch)
    # -----------------------------------------------------------------------
    def test_12_direct_delivery_execution_with_fabricated_input_sha256_mismatch(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Draft proposal and register acceptance with legitimate dataset
        res_prop = registry.execute_tool(
            "draft_commercial_proposal",
            {"client_name": "Authentic Corp", "service_tier": "data_analytics", "price_inr": 5000.0},
        )
        prop_id = res_prop.data["proposal_id"]
        original_csv = "id,metric\n1,100\n2,200"
        registry.execute_tool(
            "register_customer_order_acceptance",
            {
                "client_name": "Authentic Corp",
                "proposal_id": prop_id,
                "acceptance_ref": "PO-AUTH-01",
                "input_csv_content": original_csv,
            },
        )

        # Attempt execution with fabricated/tampered dataset
        fabricated_csv = "id,metric\n1,99999\n2,88888"
        with self.assertRaises(ValueError) as ctx:
            registry.get_tool("execute_data_workflow").handler(
                client_name="Authentic Corp",
                service_tier="data_analytics",
                input_csv_content=fabricated_csv,
            )
        self.assertIn("Dataset integrity verification failed", str(ctx.exception))
        self.assertIn("does not match the accepted order dataset", str(ctx.exception))

    # -----------------------------------------------------------------------
    # Regression Test 4 (Audit Item 4): Reusing owner approval with changed price or SLA
    # -----------------------------------------------------------------------
    def test_13_reusing_owner_approval_with_changed_price_or_sla(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Escalate discount for ₹3,000
        args_approved = {"client_name": "Bargain Corp", "service_tier": "data_analytics", "price_inr": 3000.0}
        esc_id = controller.record_escalation("draft_commercial_proposal", args_approved, "Special 40% discount")
        controller.approve_escalation(esc_id, owner_notes="Approved for Bargain Corp at ₹3000")

        # Caller attempts to use this approval with a DIFFERENT price (₹2,500)
        args_tampered_price = {"client_name": "Bargain Corp", "service_tier": "data_analytics", "price_inr": 2500.0}
        res_tampered = registry.execute_tool("draft_commercial_proposal", args_tampered_price)
        self.assertTrue(res_tampered.blocked_by_permission)
        self.assertIn("discounted >20%", res_tampered.escalation_reason)

        # Rush SLA without approval is blocked
        args_rush_sla = {"client_name": "Bargain Corp", "service_tier": "data_analytics", "price_inr": 5000.0, "sla_hours": 12.0}
        res_rush = registry.execute_tool("draft_commercial_proposal", args_rush_sla)
        self.assertTrue(res_rush.blocked_by_permission)
        self.assertIn("under standard 24h limit", res_rush.escalation_reason)

    # -----------------------------------------------------------------------
    # Regression Test 5 (Audit Item 5): Reusing approval twice (single-use consumption)
    # -----------------------------------------------------------------------
    def test_14_reusing_approval_twice_single_use_consumption(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        args = {"client_name": "Single Use Client", "service_tier": "data_analytics", "price_inr": 3500.0}
        esc_id = controller.record_escalation("draft_commercial_proposal", args, "Introductory pricing")
        controller.approve_escalation(esc_id, owner_notes="One-time exception")

        # First execution succeeds and atomically consumes the approval
        res_first = registry.execute_tool("draft_commercial_proposal", args)
        self.assertTrue(res_first.success)

        # Second execution with identical args MUST be blocked (cannot reuse consumed approval)
        res_second = registry.execute_tool("draft_commercial_proposal", args)
        self.assertTrue(res_second.blocked_by_permission)
        self.assertIn("discounted >20%", res_second.escalation_reason)

    # -----------------------------------------------------------------------
    # Regression Test 6 (Audit Item 6): Sandbox event attempting to settle production invoice and vice versa
    # -----------------------------------------------------------------------
    def test_15_sandbox_event_attempting_to_settle_production_invoice_and_vice_versa(self):
        secret = "strong_test_secret_for_environment_cross_tests_99"
        handler_sandbox = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        handler_prod = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=False)

        # 1. Create a production invoice
        _, _, inv_prod = self._create_test_invoice(client_name="Prod Client", amount=6000.0, is_sandbox=False)
        payload_prod = json.dumps({"invoice_number": inv_prod["invoice_number"], "amount_inr": 6000.0, "transaction_ref": "UTR-PROD-01"}).encode("utf-8")
        sig_prod = hmac.new(secret.encode("utf-8"), payload_prod, hashlib.sha256).hexdigest()

        # Sandbox handler attempts to process production invoice -> fails closed!
        with self.assertRaises(ValueError) as ctx_sb:
            handler_sandbox.process_webhook_event("evt_sb_on_prod", payload_prod, signature=sig_prod)
        self.assertIn("not found in sandbox environment", str(ctx_sb.exception))

        # 2. Create a sandbox invoice
        _, _, inv_sb = self._create_test_invoice(client_name="Sandbox Client", amount=4000.0, is_sandbox=True)
        payload_sb = json.dumps({"invoice_number": inv_sb["invoice_number"], "amount_inr": 4000.0, "transaction_ref": "UTR-SB-01"}).encode("utf-8")
        sig_sb = hmac.new(secret.encode("utf-8"), payload_sb, hashlib.sha256).hexdigest()

        # Production handler attempts to process sandbox invoice -> fails closed!
        with self.assertRaises(ValueError) as ctx_pr:
            handler_prod.process_webhook_event("evt_prod_on_sb", payload_sb, signature=sig_sb)
        self.assertIn("not found in production environment", str(ctx_pr.exception))

    # -----------------------------------------------------------------------
    # Regression Test 7 (Audit Item 7): Failed payment event carrying valid signature
    # -----------------------------------------------------------------------
    def test_16_failed_payment_event_carrying_valid_signature(self):
        secret = "strong_test_secret_for_failed_events_888"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)

        _, _, inv = self._create_test_invoice(client_name="Failed Payment Client", amount=5000.0, is_sandbox=True)
        payload_failed = json.dumps({
            "status": "failed",
            "invoice_number": inv["invoice_number"],
            "amount_inr": 5000.0,
            "transaction_ref": "UTR-FAIL-01",
        }).encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), payload_failed, hashlib.sha256).hexdigest()

        with self.assertRaises(ValueError) as ctx:
            handler.process_webhook_event("evt_failed_01", payload_failed, signature=sig)
        self.assertIn("indicates an unsuccessful payment", str(ctx.exception))

        # Invoice must remain unpaid
        inv_db = get_invoice(inv["id"])
        self.assertEqual(inv_db["status"], "issued")
        self.assertEqual(float(inv_db["amount_paid"]), 0.0)

    # -----------------------------------------------------------------------
    # Regression Test 8 (Audit Item 8): Missing UTR and missing event ID
    # -----------------------------------------------------------------------
    def test_17_missing_utr_and_missing_event_id(self):
        secret = "strong_test_secret_for_missing_fields_777"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        _, _, inv = self._create_test_invoice(client_name="Missing Fields Client", amount=5000.0, is_sandbox=True)

        # A. Missing event_id
        payload_valid = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-777"}).encode("utf-8")
        sig_valid = hmac.new(secret.encode("utf-8"), payload_valid, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_eid:
            handler.process_webhook_event("", payload_valid, signature=sig_valid)
        self.assertIn("Missing required event_id", str(ctx_eid.exception))

        # B. Missing transaction_ref (UTR) - never synthesize timestamp UTR
        payload_no_utr = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0}).encode("utf-8")
        sig_no_utr = hmac.new(secret.encode("utf-8"), payload_no_utr, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_utr:
            handler.process_webhook_event("evt_no_utr", payload_no_utr, signature=sig_no_utr)
        self.assertIn("Missing required bank transaction reference", str(ctx_utr.exception))

    # -----------------------------------------------------------------------
    # Regression Test 9 (Audit Item 9): Underpayment, overpayment, NaN, and infinity
    # -----------------------------------------------------------------------
    def test_18_underpayment_overpayment_nan_and_infinity(self):
        secret = "strong_test_secret_for_amount_validation_666"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        _, _, inv = self._create_test_invoice(client_name="Amount Client", amount=5000.0, is_sandbox=True)

        # 1. Underpayment (₹4,999 on ₹5,000)
        p_under = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 4999.0, "transaction_ref": "UTR-UNDER"}).encode("utf-8")
        sig_under = hmac.new(secret.encode("utf-8"), p_under, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_under:
            handler.process_webhook_event("evt_under", p_under, signature=sig_under)
        self.assertIn("Underpayment received", str(ctx_under.exception))

        # 2. Overpayment (₹5,001 on ₹5,000)
        p_over = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5001.0, "transaction_ref": "UTR-OVER"}).encode("utf-8")
        sig_over = hmac.new(secret.encode("utf-8"), p_over, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_over:
            handler.process_webhook_event("evt_over", p_over, signature=sig_over)
        self.assertIn("Overpayment received", str(ctx_over.exception))

        # 3. NaN amount
        p_nan = b'{"invoice_number": "' + inv["invoice_number"].encode() + b'", "amount_inr": NaN, "transaction_ref": "UTR-NAN"}'
        sig_nan = hmac.new(secret.encode("utf-8"), p_nan, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_nan:
            handler.process_webhook_event("evt_nan", p_nan, signature=sig_nan)
        self.assertTrue("non-finite" in str(ctx_nan.exception) or "Malformed JSON" in str(ctx_nan.exception))

        # 4. Infinity amount
        p_inf = b'{"invoice_number": "' + inv["invoice_number"].encode() + b'", "amount_inr": Infinity, "transaction_ref": "UTR-INF"}'
        sig_inf = hmac.new(secret.encode("utf-8"), p_inf, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx_inf:
            handler.process_webhook_event("evt_inf", p_inf, signature=sig_inf)
        self.assertTrue("non-finite" in str(ctx_inf.exception) or "Malformed JSON" in str(ctx_inf.exception))

    # -----------------------------------------------------------------------
    # Regression Test 10 (Audit Item 10): Reusing one UTR under different event IDs
    # -----------------------------------------------------------------------
    def test_19_reusing_one_utr_under_different_event_ids(self):
        secret = "strong_test_secret_for_utr_uniqueness_555"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        _, _, inv1 = self._create_test_invoice(client_name="Client 1", amount=5000.0, is_sandbox=True)
        _, _, inv2 = self._create_test_invoice(client_name="Client 2", amount=5000.0, is_sandbox=True)

        # Event 1 settles inv1 with UTR-SHARED-123
        p1 = json.dumps({"invoice_number": inv1["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-SHARED-123"}).encode("utf-8")
        sig1 = hmac.new(secret.encode("utf-8"), p1, hashlib.sha256).hexdigest()
        res1 = handler.process_webhook_event("evt_event_alpha", p1, signature=sig1)
        self.assertEqual(res1["status"], "settled")

        # Event 2 with DIFFERENT event_id attempts to use the SAME UTR-SHARED-123 for inv2
        p2 = json.dumps({"invoice_number": inv2["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-SHARED-123"}).encode("utf-8")
        sig2 = hmac.new(secret.encode("utf-8"), p2, hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError) as ctx:
            handler.process_webhook_event("evt_event_beta", p2, signature=sig2)
        self.assertIn("Duplicate transaction reference", str(ctx.exception))
        self.assertIn("UTR-SHARED-123", str(ctx.exception))

    # -----------------------------------------------------------------------
    # Regression Test 11 (Audit Item 11): Concurrent duplicate webhook delivery
    # -----------------------------------------------------------------------
    def test_20_concurrent_duplicate_webhook_delivery(self):
        secret = "strong_test_secret_concurrent_webhooks_444"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        _, _, inv = self._create_test_invoice(client_name="Concurrent Webhook Client", amount=5000.0, is_sandbox=True)

        payload = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-CONC-444"}).encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

        def _send_webhook():
            try:
                return handler.process_webhook_event("evt_conc_444", payload, signature=sig)
            except Exception as e:
                return {"error": str(e)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_send_webhook) for _ in range(4)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        settled_count = sum(1 for r in results if r.get("status") == "settled")
        dup_or_err_count = sum(1 for r in results if r.get("status") in {"already_settled", "duplicate_ignored"} or "error" in r)

        self.assertEqual(settled_count, 1)
        self.assertEqual(settled_count + dup_or_err_count, 4)

    # -----------------------------------------------------------------------
    # Regression Test 12 (Audit Item 12): Concurrent single-use upload-token requests
    # -----------------------------------------------------------------------
    def test_21_concurrent_single_use_upload_token_requests(self):
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Concurrent Upload Client", validity_hours=24)
        csv_bytes = b"id,val\n1,100"

        def _upload():
            try:
                return portal.validate_and_ingest_file(token, "test.csv", csv_bytes)
            except Exception as e:
                return {"error": str(e)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_upload) for _ in range(4)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        success_count = sum(1 for r in results if r.get("status") == "ingested")
        rejected_count = sum(1 for r in results if "error" in r and "already been used" in r["error"])

        self.assertEqual(success_count, 1)
        self.assertEqual(rejected_count, 3)

    # -----------------------------------------------------------------------
    # Regression Test 13 (Audit Item 13): Paid invoice belonging to a different task
    # -----------------------------------------------------------------------
    def test_22_paid_invoice_belonging_to_different_task(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Create Task 1 & paid Invoice 1
        _, t1, inv1 = self._create_test_invoice(client_name="Client 1", amount=5000.0, is_sandbox=True)

        # Settle Invoice 1
        secret = "secret_t22"
        h = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        p1 = json.dumps({"invoice_number": inv1["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-T22-01"}).encode()
        h.process_webhook_event("evt_t22_01", p1, signature=hmac.new(secret.encode(), p1, hashlib.sha256).hexdigest())

        # Create Task 2 & unpaid Invoice 2
        _, t2, inv2 = self._create_test_invoice(client_name="Client 2", amount=5000.0, is_sandbox=True)

        # Attempt to release Task 2 using Invoice 1's number -> blocked!
        res_cross = registry.execute_tool("release_completed_deliverables", {"task_id": t2, "invoice_number": inv1["invoice_number"]})
        self.assertTrue(res_cross.blocked_by_permission)
        self.assertIn("matching Task #", res_cross.escalation_reason)

    # -----------------------------------------------------------------------
    # Regression Test 14 (Audit Item 14): Download-token expiry, tampering, and replay
    # -----------------------------------------------------------------------
    def test_23_download_token_expiry_tampering_and_replay(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        opp, t_id, inv = self._create_test_invoice(client_name="DL Client", amount=5000.0, is_sandbox=True, create_artifact=False)

        # Write custom content to task's artifact_path
        t = get_task(t_id)
        pkg = Path(t["artifact_path"])
        content = b"PK\x05\x06" + b"TEST_PAYLOAD" + b"\x00" * 10
        pkg.write_bytes(content)
        expected_sha = hashlib.sha256(content).hexdigest().upper()

        # Settle invoice
        secret = "secret_t23"
        h = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)
        p = json.dumps({"invoice_number": inv["invoice_number"], "amount_inr": 5000.0, "transaction_ref": "UTR-T23"}).encode()
        h.process_webhook_event("evt_t23", p, signature=hmac.new(secret.encode(), p, hashlib.sha256).hexdigest())

        # Release deliverable
        rel = registry.execute_tool("release_completed_deliverables", {"task_id": t_id, "invoice_number": inv["invoice_number"]})
        self.assertTrue(rel.success)
        token = rel.data["download_token"]

        # A. Tampered token rejected
        with self.assertRaises(ValueError) as ctx_tamper:
            download_client_deliverable("dlv_tampered_invalid_token", is_sandbox=True)
        self.assertIn("Invalid or tampered", str(ctx_tamper.exception))

        # B. Valid download succeeds and logs event
        file_bytes, filename, file_sha = download_client_deliverable(token, is_sandbox=True, request_ip="10.0.0.1")
        self.assertEqual(file_bytes, content)
        self.assertEqual(file_sha, expected_sha)

        with connect() as conn:
            evt = conn.execute("SELECT * FROM friday_delivery_events WHERE token = ?", (token,)).fetchone()
            self.assertIsNotNone(evt)
            self.assertEqual(evt["status"], "success")
            self.assertEqual(evt["ip_or_recipient"], "10.0.0.1")

        # C. Replay attempt rejected (single-use)
        with self.assertRaises(ValueError) as ctx_replay:
            download_client_deliverable(token, is_sandbox=True)
        self.assertIn("already been used", str(ctx_replay.exception))

        # D. Expired token rejected
        with connect() as conn:
            conn.execute(
                "UPDATE friday_deliverable_tokens SET is_downloaded = 0, download_count = 0, expires_at = '2020-01-01T00:00:00+00:00' WHERE token = ?",
                (token,),
            )
            conn.commit()

        with self.assertRaises(ValueError) as ctx_exp:
            download_client_deliverable(token, is_sandbox=True)
        self.assertIn("expired", str(ctx_exp.exception))

    # -----------------------------------------------------------------------
    # Regression Test 15 (Audit Item 15): Outreach message reported truthfully without provider
    # -----------------------------------------------------------------------
    def test_24_outreach_message_reported_truthfully_without_provider(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # Clear outreach env vars if set
        orig_provider = os.environ.get("SOLOEARN_OUTREACH_PROVIDER")
        orig_ack = os.environ.get("SOLOEARN_OUTREACH_ACK")
        try:
            os.environ.pop("SOLOEARN_OUTREACH_PROVIDER", None)
            os.environ.pop("SOLOEARN_OUTREACH_ACK", None)

            # In unconfigured environment, dispatch_outreach_pitch records draft, NEVER claims dispatched
            res = registry.get_tool("dispatch_outreach_pitch").handler(
                client_name="Prospect Inc",
                channel="email",
                pitch_text="Hello, we offer automated analytics pipelines.",
            )
            self.assertEqual(res["status"], "drafted_no_provider_configured")
            self.assertEqual(res["provider"], "unconfigured")
            self.assertEqual(res["dispatch_id"], "")

            with connect() as conn:
                log = conn.execute("SELECT * FROM friday_communication_logs WHERE client_name = 'Prospect Inc'").fetchone()
                self.assertIsNotNone(log)
                self.assertEqual(log["status"], "drafted_no_provider_configured")

            # Unsupported channel rejected
            with self.assertRaises(ValueError) as ctx_unsupp:
                registry.get_tool("dispatch_outreach_pitch").handler(
                    client_name="Prospect Inc",
                    channel="fax",
                    pitch_text="Hello",
                )
            self.assertIn("Unsupported outreach channel", str(ctx_unsupp.exception))

            # When provider is configured with ack
            os.environ["SOLOEARN_OUTREACH_PROVIDER"] = "mock_sendgrid"
            os.environ["SOLOEARN_OUTREACH_ACK"] = "ack_sg_12345"
            res_ack = registry.get_tool("dispatch_outreach_pitch").handler(
                client_name="Prospect Inc",
                channel="email",
                pitch_text="Hello, with real ack.",
            )
            self.assertEqual(res_ack["status"], "dispatched")
            self.assertTrue(res_ack["dispatch_id"].startswith("MSG-"))
        finally:
            if orig_provider:
                os.environ["SOLOEARN_OUTREACH_PROVIDER"] = orig_provider
            else:
                os.environ.pop("SOLOEARN_OUTREACH_PROVIDER", None)
            if orig_ack:
                os.environ["SOLOEARN_OUTREACH_ACK"] = orig_ack
            else:
                os.environ.pop("SOLOEARN_OUTREACH_ACK", None)

    # -----------------------------------------------------------------------
    # Regression Test 16 (Audit Item 16): LLM requesting registered but out-of-sequence tool
    # -----------------------------------------------------------------------
    def test_25_llm_requesting_registered_but_out_of_sequence_tool(self):
        controller = FridayPermissionController(is_sandbox=True)
        registry = FridayToolRegistry(controller)

        # 1. LLM attempts to execute workflow before order acceptance
        res_wf = registry.execute_tool(
            "execute_data_workflow",
            {
                "client_name": "Out Of Sequence Corp",
                "service_tier": "data_analytics",
                "input_csv_content": "id,val\n1,10",
            },
        )
        self.assertFalse(res_wf.success)
        self.assertIn("Precondition failed", res_wf.error_message)
        self.assertIn("No accepted customer order found", res_wf.error_message)

        # 2. LLM attempts to audit deliverables for a non-existent task
        res_qa = registry.execute_tool(
            "audit_and_verify_deliverables",
            {"task_id": 999999},
        )
        self.assertFalse(res_qa.success)
        self.assertIn("Precondition failed", res_qa.error_message)
        self.assertIn("does not exist", res_qa.error_message)

        # 3. LLM attempts to release deliverables for a non-existent task
        res_rel = registry.execute_tool(
            "release_completed_deliverables",
            {"task_id": 888888, "invoice_number": "INV-NONE"},
        )
        self.assertFalse(res_rel.success)
        self.assertIn("Precondition failed", res_rel.error_message)
        self.assertIn("does not exist", res_rel.error_message)


if __name__ == "__main__":
    unittest.main()
