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

    def test_real_smb_pilot_targets_seeding_and_milestones(self):
        from core.commercial_funnel import (
            REAL_SMB_PILOT_TARGETS,
            get_commercial_pilot_milestones,
            list_commercial_leads,
            seed_real_smb_pilot_leads,
        )

        self.assertEqual(len(REAL_SMB_PILOT_TARGETS), 5)

        strategic = [t for t in REAL_SMB_PILOT_TARGETS if t["account_classification"] == "Strategic Account"]
        pilot = [t for t in REAL_SMB_PILOT_TARGETS if t["account_classification"] == "Initial Pilot Prospect"]
        self.assertEqual(len(strategic), 3)  # Snitch, Blue Tokai, Mokobara
        self.assertEqual(len(pilot), 2)  # Slurrp Farm, Koparo Clean

        # Verify verified co-founders, unverified pain point hypothesis, and question-led pitches
        for target in REAL_SMB_PILOT_TARGETS:
            self.assertIn("verified_founders", target)
            self.assertTrue(len(target["verified_founders"]) > 5)
            self.assertFalse(target["is_pain_point_verified"])
            self.assertEqual(
                target["hypothesis_label"],
                "Unverified Prospecting Hypothesis — Pending Discovery Conversation",
            )
            self.assertIn("Quick question:", target["custom_pitch"])
            self.assertIn("Would you", target["custom_pitch"])

        added = seed_real_smb_pilot_leads()
        self.assertEqual(added, 5)
        leads = list_commercial_leads()
        self.assertEqual(len(leads), 5)

        # All 5 seeded as prospective_account
        for l in leads:
            self.assertEqual(l["stage"], "prospective_account")
            self.assertIn("Unverified Prospecting Hypothesis", l["notes"])

        # Idempotent re-seed
        added_again = seed_real_smb_pilot_leads()
        self.assertEqual(added_again, 0)

        # Milestones
        milestones = get_commercial_pilot_milestones()
        self.assertEqual(len(milestones), 6)
        self.assertFalse(milestones[0]["achieved"])
        self.assertEqual(milestones[0]["milestone"], "1. First Customer")

    def test_section_393_statutory_tds_table_6_rates(self):
        from core.tax_engine import SECTION_393_TDS_SCHEDULE, calculate_section_393_tds

        # Statutory Table 6 verification:
        # Table 6(i): Payee is Resident Individual/HUF -> 2%
        self.assertEqual(SECTION_393_TDS_SCHEDULE["contractor_individual_payee_2"]["rate"], 0.02)
        self.assertEqual(SECTION_393_TDS_SCHEDULE["contractor_individual_payee_2"]["payee_type"], "individual_or_huf")

        # Table 6(i): Payee is other than Individual/HUF (Company/LLP/Firm) -> 1%
        self.assertEqual(SECTION_393_TDS_SCHEDULE["contractor_other_payee_1"]["rate"], 0.01)
        self.assertEqual(SECTION_393_TDS_SCHEDULE["contractor_other_payee_1"]["payee_type"], "other_than_individual")

        # Table 6(iii): Professional services -> 10%
        self.assertEqual(SECTION_393_TDS_SCHEDULE["professional_services_10"]["rate"], 0.10)

        # Technical services -> 2%
        self.assertEqual(SECTION_393_TDS_SCHEDULE["technical_services_2"]["rate"], 0.02)

        # Nil / Sub-threshold -> 0%
        self.assertEqual(SECTION_393_TDS_SCHEDULE["no_tds_0"]["rate"], 0.0)

        # Exact deduction math
        inv_subtotal = 50000.0
        self.assertEqual(calculate_section_393_tds(inv_subtotal, "contractor_individual_payee_2"), 1000.0)
        self.assertEqual(calculate_section_393_tds(inv_subtotal, "contractor_other_payee_1"), 500.0)
        self.assertEqual(calculate_section_393_tds(inv_subtotal, "professional_services_10"), 5000.0)
        self.assertEqual(calculate_section_393_tds(inv_subtotal, "technical_services_2"), 1000.0)
        self.assertEqual(calculate_section_393_tds(inv_subtotal, "no_tds_0"), 0.0)

        with self.assertRaises(ValueError):
            calculate_section_393_tds(inv_subtotal, "invalid_category")

    def test_prospective_vs_qualified_leads_distinction(self):
        from core.commercial_funnel import get_commercial_performance_dashboard

        # Seed prospective account
        add_commercial_lead(
            company_name="Prospective SMB",
            contact_name="Co-Founder",
            stage="prospective_account",
            proposed_price_inr=5000.0,
        )
        # Seed qualified lead
        add_commercial_lead(
            company_name="Qualified Client",
            contact_name="CEO",
            stage="qualified_lead",
            proposed_price_inr=10000.0,
        )

        f_metrics = get_funnel_metrics()
        self.assertEqual(f_metrics["prospective_count"], 1)
        self.assertEqual(f_metrics["qualified_count"], 1)
        self.assertEqual(f_metrics["identified_count"], 2)

        dash = get_commercial_performance_dashboard()
        self.assertEqual(dash["prospective_accounts"], 1)
        self.assertEqual(dash["qualified_leads"], 1)
        self.assertEqual(dash["pilot_readiness_score"], 7.5)


if __name__ == "__main__":
    unittest.main()
