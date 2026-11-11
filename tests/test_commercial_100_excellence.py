from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.config
from core.commercial_funnel import (
    add_commercial_lead,
    get_100_point_scorecard,
    get_5_approval_gates_status,
    get_commercial_validation_dashboard,
    init_commercial_tables,
    list_commercial_leads,
    list_outreach_interactions,
    record_outreach_interaction,
    seed_real_smb_pilot_leads,
    update_lead_stage,
)
from core.db import (
    add_opportunity,
    connect,
    create_invoice,
    create_task,
    init_db,
    set_bank_opening_balance,
    update_opportunity_status,
    verify_invoice_payment,
)
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    record_ledger_transaction,
    register_expense,
)


class Commercial100ExcellenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_path = core.config.DB_PATH
        core.config.DB_PATH = Path(self.tmp_dir.name) / "test_excellence.sqlite3"
        init_db()
        init_commercial_tables()

    def tearDown(self):
        core.config.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_live_dashboard_14_metrics_unvarnished_baseline(self):
        """Verifies that an unseeded live system displays 100% honest zero baseline across all 14 metrics."""
        dash = get_commercial_validation_dashboard(include_sandbox=False)
        self.assertEqual(dash["data_mode"], "LIVE_BUSINESS_RECORDS")
        self.assertFalse(dash["include_sandbox"])

        m = dash["metrics"]
        self.assertEqual(m["qualified_prospects"], 0)
        self.assertEqual(m["outreach_attempts"], 0)
        self.assertEqual(m["replies_and_conversations"], 0)
        self.assertEqual(m["proposals_issued"], 0)
        self.assertEqual(m["paying_customers"], 0)
        self.assertEqual(m["accepted_projects"], 0)
        self.assertEqual(m["repeat_customers"], 0)
        self.assertEqual(m["cash_actually_received_inr"], 0.0)
        self.assertEqual(m["outstanding_invoices_inr"], 0.0)
        self.assertEqual(m["contribution_per_project_inr"], 0.0)
        self.assertEqual(m["customer_acquisition_cost_inr"], 0.0)
        self.assertEqual(m["operating_expenses_inr"], 0.0)
        self.assertEqual(m["tax_provisions_inr"], 0.0)
        self.assertEqual(m["actual_post_tax_owner_profit_inr"], 0.0)

    def test_sandbox_data_isolation(self):
        """Verifies that creating simulated test data does NOT contaminate live dashboard metrics."""
        # 1. Create a sandbox lead
        lead_id = add_commercial_lead(
            company_name="Simulated Brand Pvt Ltd",
            contact_name="Test Founder",
            stage="paid_pilot",
            is_sandbox=True,
        )

        # 2. Record a sandbox outreach interaction
        record_outreach_interaction(
            lead_id=lead_id,
            interaction_type="outreach_sent",
            channel="linkedin",
            decision_maker_name="Test Founder",
            message_summary="Simulated pitch",
            is_sandbox=True,
        )

        # 3. Create sandbox expense and transaction
        register_expense(
            vendor="Sandbox Cloud",
            category="software_tool",
            amount_inr=1500.0,
            is_sandbox=True,
        )
        record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=5000.0,
            description="Sandbox client payment",
            reference_id="INV-SB-001",
            is_sandbox=True,
        )

        # Check LIVE metrics (include_sandbox=False): MUST BE CLEAN ZERO
        live_dash = get_commercial_validation_dashboard(include_sandbox=False)
        lm = live_dash["metrics"]
        self.assertEqual(lm["qualified_prospects"], 0)
        self.assertEqual(lm["outreach_attempts"], 0)
        self.assertEqual(lm["paying_customers"], 0)
        self.assertEqual(lm["cash_actually_received_inr"], 0.0)
        self.assertEqual(lm["operating_expenses_inr"], 0.0)
        self.assertEqual(lm["actual_post_tax_owner_profit_inr"], 0.0)

        # Check SANDBOX metrics (include_sandbox=True): MUST REFLECT SIMULATED ACTIVITY
        sb_dash = get_commercial_validation_dashboard(include_sandbox=True)
        sm = sb_dash["metrics"]
        self.assertEqual(sm["qualified_prospects"], 1)
        self.assertEqual(sm["outreach_attempts"], 1)
        self.assertEqual(sm["cash_actually_received_inr"], 5000.0)
        self.assertEqual(sm["operating_expenses_inr"], 1500.0)
        self.assertEqual(sm["actual_post_tax_owner_profit_inr"], 3500.0)

    def test_100_point_scorecard_structure_and_weights(self):
        """Verifies the 6 Core Objectives, weights totaling 100, and verified baseline score."""
        scorecard = get_100_point_scorecard(include_sandbox=False)
        self.assertEqual(scorecard["max_score"], 100.0)

        pillars = scorecard["pillars"]
        self.assertEqual(len(pillars), 6)

        total_weight = sum(p["weight"] for p in pillars)
        self.assertEqual(total_weight, 100.0)

        # Check individual pillar weights
        p_map = {p["name"]: p for p in pillars}
        self.assertEqual(p_map["Product Reliability & Security"]["weight"], 15.0)
        self.assertEqual(p_map["Customer Acquisition & Sales Conversion"]["weight"], 25.0)
        self.assertEqual(p_map["Delivery Quality & Customer Satisfaction"]["weight"], 20.0)
        self.assertEqual(p_map["Accurate Financial Accounting & Profitability"]["weight"], 20.0)
        self.assertEqual(p_map["Safe Automation with Human Approval"]["weight"], 10.0)
        self.assertEqual(p_map["Indian Business, Banking, Tax & Data Compliance"]["weight"], 10.0)

        # Live verified score baseline: 76.0 pts
        self.assertEqual(scorecard["total_score"], 76.0)
        self.assertEqual(scorecard["completion_pct"], 76.0)
        self.assertIn("Commercial Baseline Verified", scorecard["verdict"])

    def test_5_human_approval_gates_enforcement(self):
        """Verifies that all 5 Human Approval Gates are defined, marked ENFORCED, and cannot be bypassed."""
        gates = get_5_approval_gates_status()
        self.assertEqual(len(gates), 5)

        for g in gates:
            self.assertEqual(g["status"], "ENFORCED")
            self.assertFalse(g["bypass_permitted"])
            self.assertIn("rule", g)
            self.assertIn("enforcement_point", g)

        names = [g["name"] for g in gates]
        self.assertIn("Gate 1: Outbound Proposal Approval", names)
        self.assertIn("Gate 2: Contract & Scope Authorization", names)
        self.assertIn("Gate 3: Bank Settlement & Reconciliation", names)
        self.assertIn("Gate 4: Refund & Credit Note Authorization", names)
        self.assertIn("Gate 5: Client Deliverable Sign-off", names)

    def test_outreach_interaction_logging_and_stage_progression(self):
        """Verifies logging outreach interactions, capturing decision-maker title, and recording loss reasons."""
        lead_id = add_commercial_lead(
            company_name="Snitch Apparels Pvt Ltd",
            contact_name="Siddharth Dungarwal",
            contact_channel="linkedin",
            stage="prospective_account",
            decision_maker_title="Founder & CEO",
        )

        # 1. Log outreach sent
        oi_id1 = record_outreach_interaction(
            lead_id=lead_id,
            interaction_type="outreach_sent",
            channel="linkedin",
            decision_maker_name="Siddharth Dungarwal",
            decision_maker_title="Founder & CEO",
            message_summary="Sent question-led pitch on multi-channel retail CSV consolidation.",
            outcome="pending",
        )
        self.assertGreater(oi_id1, 0)
        update_lead_stage(lead_id, "approached")

        # 2. Log reply received
        oi_id2 = record_outreach_interaction(
            lead_id=lead_id,
            interaction_type="reply_received",
            channel="linkedin",
            decision_maker_name="Siddharth Dungarwal",
            decision_maker_title="Founder & CEO",
            message_summary="Replied asking to see sample deliverables workbook.",
            outcome="positive_reply",
        )
        update_lead_stage(lead_id, "in_conversation")

        # Retrieve and verify interaction history
        interactions = list_outreach_interactions(lead_id=lead_id)
        self.assertEqual(len(interactions), 2)
        self.assertEqual(interactions[0]["interaction_type"], "reply_received")
        self.assertEqual(interactions[1]["interaction_type"], "outreach_sent")

        # Verify dashboard metrics update
        dash = get_commercial_validation_dashboard(include_sandbox=False)
        m = dash["metrics"]
        self.assertEqual(m["outreach_attempts"], 1)
        self.assertEqual(m["replies_and_conversations"], 1)


if __name__ == "__main__":
    unittest.main()
