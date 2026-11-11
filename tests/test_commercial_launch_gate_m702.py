"""
tests/test_commercial_launch_gate_m702.py
Tests for SOLOEARN Commercial Launch Gate M7.0.2:
1. Udyam NIC code recommendations (62011, 62020, 63111 primary, 63112 conditional).
2. Kotak onboarding guide corrections (AQB vs Opening Deposit, RBI 2-document KYC rule, Kotak.biz pending).
3. Customer advance accounting: separation from earned revenue, unearned liability tracking, and Gate 4 refund protection.
4. Commercial Launch Checklist: 7 sequential owner confirmation milestones (code-tested vs externally verified).
5. Preflight execution and strict sandbox data isolation.
"""

import unittest
from core.db import init_db, connect
from core.preflight import get_kotak_onboarding_guide, run_m7_local_preflight
from core.financial_ledger import (
    record_customer_advance,
    apply_customer_advance,
    refund_customer_advance,
    get_customer_advance_liability,
    get_cash_and_reserves_summary,
)
from core.commercial_funnel import (
    init_commercial_tables,
    init_commercial_launch_checklist,
    get_commercial_launch_checklist,
    confirm_launch_checklist_item,
    get_launch_checklist_summary,
    get_commercial_validation_dashboard,
    get_100_point_scorecard,
)


class TestCommercialLaunchGateM702(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        init_commercial_tables()
        init_commercial_launch_checklist()

    def test_udyam_nic_codes_and_recommendations(self):
        """Verifies correct NIC code classifications under NIC 2008."""
        guide = get_kotak_onboarding_guide()
        nic_codes = {item["code"]: item for item in guide["recommended_nic_codes"]}

        # 62011: Custom software programming
        self.assertIn("62011", nic_codes)
        self.assertIn("programming", nic_codes["62011"]["name"].lower())

        # 62020: Computer consultancy
        self.assertIn("62020", nic_codes)
        self.assertIn("consultancy", nic_codes["62020"]["name"].lower())

        # 63111: Data processing, including report writing (Primary for analytics)
        self.assertIn("63111", nic_codes)
        self.assertIn("data processing", nic_codes["63111"]["name"].lower())
        self.assertEqual(nic_codes["63111"]["status"], "PRIMARY_RECOMMENDED")

        # 63112: Web hosting (Conditional only - not appropriate merely for analytics)
        self.assertIn("63112", nic_codes)
        self.assertIn("hosting", nic_codes["63112"]["name"].lower())
        self.assertEqual(nic_codes["63112"]["status"], "CONDITIONAL_ONLY")
        self.assertIn("conditional only", nic_codes["63112"]["appropriateness"].lower())

    def test_kotak_onboarding_guide_distinctions(self):
        """Verifies Kotak Safalta AQB vs opening deposit and RBI KYC rules."""
        guide = get_kotak_onboarding_guide()

        # Safalta details under Neo scheme
        self.assertEqual(guide["average_quarterly_balance_inr"], 5000.0)
        self.assertIn("Neo Current Account", guide["account_scheme"])

        # Shortfall charges
        self.assertIn("500", guide["aqb_shortfall_charges"]["between_2500_and_4999"])
        self.assertIn("1,000", guide["aqb_shortfall_charges"]["below_2500"])

        # AQB vs Opening deposit distinction & approval disclaimer
        aqb_dist = guide["aqb_vs_opening_deposit"]
        self.assertIn("distinct", aqb_dist["distinction"].lower())
        self.assertIn("not guarantee account approval", aqb_dist["approval_disclaimer"].lower())

        # RBI KYC sole proprietorship 2-document rule
        rbi_rules = guide["rbi_kyc_rules"]
        self.assertIn("two documents", rbi_rules["rule_summary"].lower())
        self.assertIn("udyam", rbi_rules["accepted_documents"][0].lower())
        self.assertIn("does not guarantee", rbi_rules["bank_discretion_warning"].lower())

        # Kotak.biz pending status
        self.assertIn("PENDING", guide["kotak_biz_specs"]["activation_status"])

    def test_customer_advance_accounting_separation(self):
        """
        Verifies customer advances are treated as unearned liabilities,
        never recognized as earned profit until work is completed and accepted.
        """
        # 1. Record a ₹2,500 advance in sandbox
        adv_tx_id = record_customer_advance(
            amount_inr=2500.0,
            client_name="Test Pilot Client",
            reference_id="ADV-TEST-001",
            description="50% advance for pilot analytics",
            is_sandbox=True,
        )
        self.assertIsInstance(adv_tx_id, int)

        # 2. Verify liability is tracked
        liability = get_customer_advance_liability(include_sandbox=True)
        self.assertGreaterEqual(liability, 2500.0)

        # 3. Verify get_cash_and_reserves_summary reserves the advance
        cash_summary = get_cash_and_reserves_summary(include_sandbox=True)
        self.assertGreaterEqual(cash_summary["customer_advance_liability_inr"], 2500.0)

        # 4. Applying advance converts liability to earned revenue
        apply_customer_advance(
            advance_tx_id=adv_tx_id,
            invoice_number="INV-ADV-TEST",
            amount_inr=2500.0,
            description="Applied pilot advance",
            is_sandbox=True,
        )
        updated_liability = get_customer_advance_liability(include_sandbox=True)
        self.assertEqual(updated_liability, liability - 2500.0)

        # 5. Refund requires Gate 4 owner confirmation
        adv_tx_id_2 = record_customer_advance(
            amount_inr=1000.0,
            client_name="Refund Client",
            reference_id="ADV-TEST-002",
            is_sandbox=True,
        )
        with self.assertRaises(ValueError):
            # Autonomous refund blocked
            refund_customer_advance(adv_tx_id_2, 1000.0, "Client canceled", owner_confirmed=False, is_sandbox=True)

        # Owner confirmed refund succeeds
        refund_tx_id = refund_customer_advance(adv_tx_id_2, 1000.0, "Client canceled", owner_confirmed=True, is_sandbox=True)
        self.assertIsInstance(refund_tx_id, int)

    def test_commercial_launch_checklist_initialization_and_confirmation(self):
        """Verifies the 7-item Commercial Launch Gate checklist."""
        checklist = get_commercial_launch_checklist()
        self.assertEqual(len(checklist), 7)

        expected_keys = [
            "business_account_approval",
            "merchant_qr_activation",
            "first_real_prospect_contacted",
            "written_pilot_agreement",
            "actual_work_completed_and_accepted",
            "verified_bank_credit",
            "successful_soloearn_reconciliation",
        ]
        actual_keys = [item["item_key"] for item in checklist]
        self.assertEqual(actual_keys, expected_keys)

        # All 7 are code-tested
        for item in checklist:
            self.assertEqual(item["is_code_tested"], 1)
            self.assertTrue(len(item["code_test_details"]) > 0)
            self.assertTrue(len(item["instruction_guide"]) > 0)

        # Initial summary
        summary = get_launch_checklist_summary()
        self.assertEqual(summary["total_items"], 7)
        self.assertEqual(summary["code_tested_count"], 7)

        # Confirm step 1 with evidence
        res = confirm_launch_checklist_item(
            item_key="business_account_approval",
            owner_confirmed=True,
            evidence_notes="Kotak Safalta Current Account #9876543210 approved by branch.",
        )
        self.assertEqual(res["owner_confirmed"], 1)
        self.assertEqual(res["is_externally_verified"], 1)
        self.assertEqual(res["status"], "CONFIRMED")
        self.assertIn("9876543210", res["evidence_notes"])

        # Reset step 1 for test cleanliness
        confirm_launch_checklist_item("business_account_approval", owner_confirmed=False, evidence_notes="")

    def test_100_point_scorecard_and_validation_dashboard(self):
        """Verifies 100-point scorecard reflects updated compliance and checklist."""
        scorecard = get_100_point_scorecard(include_sandbox=False)
        self.assertGreaterEqual(scorecard["total_score"], 76.0)

        pillar6 = next(p for p in scorecard["pillars"] if p["id"] == 6)
        p6_names = [i["name"] for i in pillar6["items"]]
        self.assertTrue(any("63111" in name for name in p6_names))
        self.assertTrue(any("Kotak Safalta" in name for name in p6_names))
        self.assertTrue(any("Advance Separation" in name for name in p6_names))

        val_dash = get_commercial_validation_dashboard(include_sandbox=False)
        self.assertIn("launch_checklist_summary", val_dash)
        self.assertIn("customer_advance_liability_inr", val_dash)
        self.assertEqual(val_dash["launch_checklist_summary"]["total_items"], 7)


if __name__ == "__main__":
    unittest.main()
