from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
import pandas as pd

import core.config
from core.commercial_funnel import (
    add_commercial_lead,
    calculate_project_unit_economics,
    get_funnel_metrics,
    init_commercial_tables,
    list_commercial_leads,
    update_lead_stage,
)
from core.db import init_db


class CommercialReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_path = core.config.DB_PATH
        core.config.DB_PATH = Path(self.tmp_dir.name) / "test_commercial.sqlite3"
        init_db()
        init_commercial_tables()

    def tearDown(self):
        core.config.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_commercial_lead_lifecycle(self):
        lead_id = add_commercial_lead(
            company_name="Apex Retailers Ltd",
            contact_name="Aman Gupta",
            contact_channel="linkedin",
            contact_handle="@amangupta",
            industry="retail",
            service_tier="data_analytics",
            proposed_price_inr=5000.0,
            stage="identified",
            notes="Initial review of 3 stores in Delhi NCR",
        )
        self.assertIsInstance(lead_id, int)
        self.assertGreater(lead_id, 0)

        # Retrieve and verify
        leads = list_commercial_leads()
        lead = next((l for l in leads if l["id"] == lead_id), None)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["company_name"], "Apex Retailers Ltd")
        self.assertEqual(lead["stage"], "identified")
        self.assertEqual(lead["proposed_price_inr"], 5000.0)

        # Transition to approached
        ok = update_lead_stage(lead_id, "approached", notes="Sent Template 2 on LinkedIn")
        self.assertTrue(ok)
        leads = list_commercial_leads(stage="approached")
        lead = next((l for l in leads if l["id"] == lead_id), None)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["stage"], "approached")
        self.assertEqual(lead["notes"], "Sent Template 2 on LinkedIn")

        # Transition to in_conversation
        update_lead_stage(lead_id, "in_conversation", notes="Agreed to review sample report")
        leads = list_commercial_leads(stage="in_conversation")
        lead = next((l for l in leads if l["id"] == lead_id), None)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["stage"], "in_conversation")

        # Transition to paid_pilot
        update_lead_stage(lead_id, "paid_pilot", notes="Booked ₹5,000 pilot with advance")
        leads = list_commercial_leads(stage="paid_pilot")
        lead = next((l for l in leads if l["id"] == lead_id), None)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["stage"], "paid_pilot")

    def test_invalid_stage_or_channel_rejected(self):
        with self.assertRaises(ValueError):
            add_commercial_lead(
                company_name="Test Corp",
                contact_name="John Doe",
                contact_channel="invalid_channel",
            )

        with self.assertRaises(ValueError):
            add_commercial_lead(
                company_name="Test Corp",
                contact_name="John Doe",
                stage="non_existent_stage",
            )

        with self.assertRaises(ValueError):
            add_commercial_lead(
                company_name="",
                contact_name="John Doe",
            )

    def test_funnel_metrics_progression(self):
        metrics = get_funnel_metrics()
        self.assertIn("identified_count", metrics)
        self.assertIn("identified_target", metrics)
        self.assertEqual(metrics["identified_target"], 50)
        self.assertEqual(metrics["approached_target"], 30)
        self.assertEqual(metrics["in_conversation_target"], 5)
        self.assertEqual(metrics["paid_pilot_target"], 3)
        self.assertIn("stage_breakdown", metrics)

    def test_unit_economics_tier1_pilot(self):
        # ₹5,000 price, 5% fee (₹250), ₹150 direct costs, 2.0 hours
        ue = calculate_project_unit_economics(
            gross_price_inr=5000.0,
            platform_fee_pct=5.0,
            direct_cost_inr=150.0,
            estimated_hours=2.0,
            other_income_inr=0.0,
        )
        self.assertEqual(ue["gross_price_inr"], 5000.0)
        self.assertEqual(ue["platform_fee_inr"], 250.0)
        self.assertEqual(ue["direct_cost_inr"], 150.0)
        self.assertEqual(ue["net_pretax_receipt_inr"], 4600.0)
        self.assertEqual(ue["contribution_margin_pct"], 92.0)
        self.assertEqual(ue["estimated_tax_reserve_inr"], 0.0)  # Section 156 full rebate
        self.assertEqual(ue["net_owner_profit_inr"], 4600.0)
        self.assertEqual(ue["hourly_yield_inr"], 2300.0)

    def test_unit_economics_invalid_inputs(self):
        with self.assertRaises(ValueError):
            calculate_project_unit_economics(gross_price_inr=-100.0)
        with self.assertRaises(ValueError):
            calculate_project_unit_economics(gross_price_inr=5000.0, estimated_hours=0.0)
        with self.assertRaises(ValueError):
            calculate_project_unit_economics(gross_price_inr=5000.0, platform_fee_pct=90.0)


if __name__ == "__main__":
    unittest.main()
