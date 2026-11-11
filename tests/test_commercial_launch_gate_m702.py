"""
tests/test_commercial_launch_gate_m702.py
Tests for SOLOEARN Commercial Launch Gate M7.0.2:
1. Udyam NIC code recommendations (62011, 62020, 63111 primary, 63112 conditional).
2. Kotak onboarding guide corrections (AQB vs Opening Deposit, RBI 2-document KYC rule, Kotak.biz pending).
3. Customer advance accounting: separation from earned revenue, unearned liability tracking, and Gate 4 refund protection.
4. Commercial Launch Checklist: 9 sequential owner confirmation milestones (code-tested vs externally verified).
5. Advance application & bank statement reconciliation without double counting.
6. Preflight execution and strict sandbox data isolation.
"""

import unittest
from datetime import datetime
from core.db import init_db, connect, add_opportunity, create_task, create_invoice
from core.preflight import get_kotak_onboarding_guide, run_m7_local_preflight
from core.financial_ledger import (
    record_customer_advance,
    apply_customer_advance,
    refund_customer_advance,
    get_customer_advance_liability,
    get_cash_and_reserves_summary,
    reconcile_statement_row,
    inr_to_paise,
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
    def _cleanup_sandbox(cls):
        with connect() as conn:
            conn.execute("DELETE FROM financial_transactions WHERE is_sandbox = 1")
            conn.execute("DELETE FROM invoices WHERE is_sandbox = 1")
            conn.execute("DELETE FROM tasks WHERE is_sandbox = 1")
            conn.execute("DELETE FROM opportunities WHERE is_sandbox = 1")
            conn.execute("DELETE FROM bank_statement_rows WHERE bank_ref LIKE 'UPI/SPLIT/%'")
            conn.execute("DELETE FROM bank_statement_imports WHERE filename = 'test_split.csv'")

    @classmethod
    def setUpClass(cls):
        init_db()
        init_commercial_tables()
        init_commercial_launch_checklist()
        cls._cleanup_sandbox()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_sandbox()

    def setUp(self):
        self._cleanup_sandbox()

    def tearDown(self):
        self._cleanup_sandbox()

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
        """Verifies the 9-item Commercial Launch Gate checklist."""
        checklist = get_commercial_launch_checklist()
        self.assertEqual(len(checklist), 9)

        expected_keys = [
            "udyam_registration",
            "kotak_written_confirmation",
            "kotak_aqb_and_charges_confirmed",
            "open_business_current_account",
            "activate_kotak_biz_merchant_qr",
            "send_first_five_approaches",
            "secure_written_pilot_agreement",
            "complete_work_and_obtain_acceptance",
            "verify_bank_credit_and_reconcile",
        ]
        actual_keys = [item["item_key"] for item in checklist]
        self.assertEqual(actual_keys, expected_keys)

        # All 9 are code-tested
        for item in checklist:
            self.assertEqual(item["is_code_tested"], 1)
            self.assertTrue(len(item["code_test_details"]) > 0)
            self.assertTrue(len(item["instruction_guide"]) > 0)

        # Initial summary
        summary = get_launch_checklist_summary()
        self.assertEqual(summary["total_items"], 9)
        self.assertEqual(summary["code_tested_count"], 9)

        # Confirm step 1 with evidence
        res = confirm_launch_checklist_item(
            item_key="udyam_registration",
            owner_confirmed=True,
            evidence_notes="UDYAM-DL-01-0012345 registered under NIC 63111.",
        )
        self.assertEqual(res["owner_confirmed"], 1)
        self.assertEqual(res["is_externally_verified"], 1)
        self.assertEqual(res["status"], "CONFIRMED")
        self.assertIn("UDYAM-DL-01-0012345", res["evidence_notes"])

        # Reset step 1 for test cleanliness
        confirm_launch_checklist_item("udyam_registration", owner_confirmed=False, evidence_notes="")

    def test_split_advance_and_statement_reconciliation_no_double_counting(self):
        """
        Verifies the end-to-end split advance lifecycle:
        1. Advance ₹2,500 received -> liability increases, cash in bank increases.
        2. Advance applied to ₹5,000 invoice -> unpaid balance drops to ₹2,500.
        3. Bank statement row for remaining ₹2,500 imported and reconciled.
        4. Verifies bank cash, recognized revenue, and invoices are NEVER double-counted.
        """
        # Step 1: Record sandbox advance of ₹2,500
        adv_tx_id = record_customer_advance(
            amount_inr=2500.0,
            client_name="Split Advance Client",
            reference_id="ADV-SPLIT-999",
            description="50% upfront deposit",
            is_sandbox=True,
        )
        self.assertIsInstance(adv_tx_id, int)

        # Step 2: Create sandbox invoice for ₹5,000 total
        opp_id = add_opportunity({
            "title": "Split Pilot Opportunity",
            "source": "verified_direct",
            "source_permitted": True,
            "amount_inr": 5000.0,
            "estimated_hours": 3.0,
            "skill_match": 0.95,
            "payment_confidence": 0.95,
            "risk_level": 1.0,
        }, score=9.5, is_sandbox=True)

        with connect() as conn:
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))

        task_id = create_task(
            title="Split Pilot Deliverable",
            task_type="csv_business_analysis",
            workspace="workspaces/split_test",
            opportunity_id=opp_id,
            is_sandbox=True,
        )

        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='submission_ready', artifact_path='workspaces/split_test/test.zip' WHERE id=?",
                (task_id,),
            )

        inv = create_invoice(
            opportunity_id=opp_id,
            client_name="Split Advance Client",
            service_tier="data_analytics",
            amount_subtotal=5000.0,
            tax_rate=0.0,
            task_id=task_id,
            require_gst_registration=True,
            is_sandbox=True,
        )
        inv_id = inv["id"]
        inv_num = inv["invoice_number"]

        try:
            # Step 3: Apply the ₹2,500 advance to the invoice
            apply_tx_id = apply_customer_advance(
                advance_tx_id=adv_tx_id,
                invoice_number=inv_num,
                amount_inr=2500.0,
                description="Apply 50% advance to invoice",
                is_sandbox=True,
            )
            self.assertIsInstance(apply_tx_id, int)

            # Verify invoice has amount_paid = ₹2,500 and status 'payment_pending'
            with connect() as conn:
                inv_row = conn.execute("SELECT amount_total, amount_paid, status FROM invoices WHERE id=?", (inv_id,)).fetchone()
                self.assertEqual(float(inv_row["amount_total"]), 5000.0)
                self.assertEqual(float(inv_row["amount_paid"]), 2500.0)
                self.assertEqual(inv_row["status"], "payment_pending")

            # Verify cash summary: unapplied advance is 0, earned revenue is 2,500
            cash_pre = get_cash_and_reserves_summary(include_sandbox=True)
            self.assertEqual(cash_pre["customer_advance_liability_inr"], 0.0)
            with connect() as conn:
                unpaid_row = conn.execute("SELECT SUM(amount_total - amount_paid) FROM invoices WHERE id=?", (inv_id,)).fetchone()
                self.assertEqual(float(unpaid_row[0]), 2500.0)

            # Step 4: Import bank statement row for remaining balance of ₹2,500
            now = datetime.now().isoformat()
            with connect() as conn:
                conn.execute(
                    """INSERT INTO bank_statement_imports (
                        filename, row_count, reconciled_count, notes, imported_at
                    ) VALUES ('test_split.csv', 1, 0, 'split test', ?)""",
                    (now,),
                )
                imp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                conn.execute(
                    """INSERT INTO bank_statement_rows (
                        import_id, tx_date, description, amount_inr, amount_paise,
                        tx_type, bank_ref, is_reconciled, confirmed_by_owner
                    ) VALUES (?, '2026-11-12', 'UPI/Kotak/SPLIT2500/Cr', 2500.0, 250000, 'CR', 'UPI/SPLIT/002', 0, 0)""",
                    (imp_id,),
                )
                st_row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Step 5: Reconcile the statement row against the invoice
            rec_res = reconcile_statement_row(
                row_id=st_row_id,
                entity_type="invoice",
                entity_id=str(inv_id),
                owner_confirmed=True,
            )
            self.assertEqual(rec_res["status"], "reconciled")

            # Verify invoice is now fully paid
            with connect() as conn:
                inv_after = conn.execute("SELECT amount_total, amount_paid, status FROM invoices WHERE id=?", (inv_id,)).fetchone()
                self.assertEqual(float(inv_after["amount_total"]), 5000.0)
                self.assertEqual(float(inv_after["amount_paid"]), 5000.0)
                self.assertEqual(inv_after["status"], "paid")

            # Step 6: Verify total cash and revenue are NOT double counted
            cash_final = get_cash_and_reserves_summary(include_sandbox=True)
            self.assertEqual(cash_final["customer_advance_liability_inr"], 0.0)
            with connect() as conn:
                unpaid_row_after = conn.execute("SELECT SUM(amount_total - amount_paid) FROM invoices WHERE id=?", (inv_id,)).fetchone()
                self.assertEqual(float(unpaid_row_after[0]), 0.0)

            # Check individual client receipts in financial_transactions for this invoice
            with connect() as conn:
                rows = conn.execute(
                    "SELECT tx_type, amount_inr FROM financial_transactions WHERE reference_id=? AND is_sandbox=1",
                    (inv_num,),
                ).fetchall()
                tx_types = [r["tx_type"] for r in rows]
                self.assertIn("customer_advance_applied", tx_types)
                self.assertIn("client_receipt", tx_types)
                # Earned client revenue: exactly ₹5,000 (₹2,500 applied advance + ₹2,500 bank credit)
                earned_client_receipts = sum(float(r["amount_inr"]) for r in rows if r["tx_type"] == "client_receipt")
                self.assertEqual(earned_client_receipts, 5000.0)
                # Contra-liability entry: exactly ₹2,500
                applied_advance_entries = sum(float(r["amount_inr"]) for r in rows if r["tx_type"] == "customer_advance_applied")
                self.assertEqual(applied_advance_entries, 2500.0)

        finally:
            # Clean up test sandbox records cleanly
            with connect() as conn:
                conn.execute("DELETE FROM financial_transactions WHERE reference_id IN (?, 'ADV-SPLIT-999') AND is_sandbox=1", (inv_num,))
                conn.execute("DELETE FROM invoices WHERE id=?", (inv_id,))
                conn.execute("DELETE FROM revenue WHERE opportunity_id=?", (opp_id,))
                conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                conn.execute("DELETE FROM opportunities WHERE id=?", (opp_id,))
                conn.execute("DELETE FROM bank_statement_rows WHERE bank_ref='UPI/SPLIT/002'")
                conn.execute("DELETE FROM bank_statement_imports WHERE filename='test_split.csv'")

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
        self.assertEqual(val_dash["launch_checklist_summary"]["total_items"], 9)


if __name__ == "__main__":
    unittest.main()
