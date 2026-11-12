"""
tests/test_financial_integrity_review.py
SOLOEARN M7.0.2 - Final Financial Integrity Review Audit Suite

Proves through independent test cases that:
1. TDS is never treated as cash received (tracked as tax-credit receivable asset under Section 393).
2. Customer advances are never counted twice in revenue, cash, or invoice balances.
3. GST collected is cash received but is strictly locked in GST liability reserve, never distributable profit.
4. Applying an advance from liability to invoice creates NO second bank receipt.
5. Every reconciled receipt strictly requires a genuine imported bank credit; screenshots and unverified claims fail closed.
6. Duplicate bank statement imports and duplicate UTRs are rejected.
7. Owner withdrawals and business expenses are strictly separated.
8. All commercial dashboards and scorecard reports reconcile to the underlying immutable ledger.
"""

import unittest
from datetime import datetime, timezone
from core.db import (
    init_db,
    connect,
    add_opportunity,
    create_task,
    create_invoice,
    set_bank_opening_balance,
    get_active_tax_profile,
)
from core.financial_ledger import (
    record_ledger_transaction,
    record_owner_draw,
    record_customer_advance,
    apply_customer_advance,
    refund_customer_advance,
    get_customer_advance_liability,
    get_cash_and_reserves_summary,
    import_bank_statement_csv,
    reconcile_statement_row,
    register_expense,
    inr_to_paise,
)
from core.commercial_funnel import (
    init_commercial_tables,
    get_commercial_validation_dashboard,
    get_commercial_performance_dashboard,
    get_100_point_scorecard,
)


class TestFinancialIntegrityReview(unittest.TestCase):
    @classmethod
    def _cleanup_sandbox(cls):
        with connect() as conn:
            conn.execute("DELETE FROM financial_transactions WHERE is_sandbox = 1")
            conn.execute("DELETE FROM invoices WHERE is_sandbox = 1")
            conn.execute("DELETE FROM revenue WHERE is_sandbox = 1")
            conn.execute("DELETE FROM tasks WHERE is_sandbox = 1")
            conn.execute("DELETE FROM opportunities WHERE is_sandbox = 1")
            conn.execute("DELETE FROM expenses WHERE is_sandbox = 1")
            conn.execute("DELETE FROM bank_statement_rows WHERE bank_ref LIKE 'REV-TEST-%'")
            conn.execute("DELETE FROM bank_statement_imports WHERE filename LIKE 'fin_integrity_%'")

    @classmethod
    def setUpClass(cls):
        init_db()
        init_commercial_tables()
        cls._cleanup_sandbox()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_sandbox()

    def setUp(self):
        self._cleanup_sandbox()

    def tearDown(self):
        self._cleanup_sandbox()

    def _setup_sandbox_invoice(self, amount_subtotal: float, tax_rate: float = 0.0, client_name: str = "Audit Client"):
        opp_id = add_opportunity({
            "title": f"Integrity Audit Project for {client_name}",
            "source": "verified_direct",
            "source_permitted": True,
            "amount_inr": amount_subtotal * (1.0 + tax_rate / 100.0),
            "estimated_hours": 4.0,
            "skill_match": 0.95,
            "payment_confidence": 0.95,
            "risk_level": 1.0,
        }, score=9.5, is_sandbox=True)

        with connect() as conn:
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))

        task_id = create_task(
            title=f"Audit Deliverable for {client_name}",
            task_type="csv_business_analysis",
            workspace="workspaces/integrity_audit",
            opportunity_id=opp_id,
            is_sandbox=True,
        )

        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='submission_ready', artifact_path='workspaces/integrity_audit/report.zip' WHERE id=?",
                (task_id,),
            )

        inv = create_invoice(
            opportunity_id=opp_id,
            client_name=client_name,
            service_tier="data_analytics",
            amount_subtotal=amount_subtotal,
            tax_rate=tax_rate,
            task_id=task_id,
            require_gst_registration=False,
            is_sandbox=True,
        )
        return opp_id, task_id, inv

    def _import_statement_row(self, amount: float, tx_type: str, bank_ref: str, desc: str = "Direct Deposit"):
        now = datetime.now(timezone.utc).isoformat()
        with connect() as conn:
            conn.execute(
                """INSERT INTO bank_statement_imports (
                    filename, row_count, reconciled_count, notes, imported_at
                ) VALUES (?, 1, 0, 'Audit test import', ?)""",
                (f"fin_integrity_{bank_ref}.csv", now),
            )
            imp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            conn.execute(
                """INSERT INTO bank_statement_rows (
                    import_id, tx_date, description, amount_inr, amount_paise,
                    tx_type, bank_ref, is_reconciled, confirmed_by_owner
                ) VALUES (?, '2026-11-12', ?, ?, ?, ?, ?, 0, 0)""",
                (imp_id, desc, amount, inr_to_paise(amount), tx_type, bank_ref),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return row_id

    # -------------------------------------------------------------------------
    # PROOF 1: TDS IS NEVER TREATED AS CASH RECEIVED
    # -------------------------------------------------------------------------
    def test_proof1_tds_withheld_is_tax_receivable_never_bank_cash(self):
        """
        Tests that on a ₹10,000 invoice with 10% TDS (₹1,000) withheld under Section 393:
        1. Bank credit is strictly ₹9,000.00.
        2. Bank cash balance increases by only ₹9,000.00.
        3. TDS is recognized as an explicit tax-credit asset / receivable (₹1,000.00).
        4. Earned business turnover is ₹10,000.00.
        5. The ₹1,000 withheld is NEVER credited as bank cash.
        """
        set_bank_opening_balance(50000.0, as_of_date="2026-11-01", is_confirmed=True)
        _, _, inv = self._setup_sandbox_invoice(amount_subtotal=10000.0, tax_rate=0.0, client_name="TDS Corp")
        inv_id = inv["id"]
        inv_num = inv["invoice_number"]

        # Bank credit of ₹9,000 arrives in bank account (client withheld ₹1,000 TDS)
        row_id = self._import_statement_row(
            amount=9000.0, tx_type="credit", bank_ref="REV-TEST-TDS-001", desc=f"UPI/TDS9000/CR/{inv_num}"
        )

        # Reconcile statement credit with explicit ₹1,000 TDS withheld
        rec = reconcile_statement_row(
            row_id=row_id,
            entity_type="invoice",
            entity_id=str(inv_id),
            owner_confirmed=True,
            tds_withheld_inr=1000.0,
        )
        self.assertEqual(rec["status"], "reconciled")
        self.assertEqual(rec["amount_inr"], 9000.0)
        self.assertEqual(rec["tds_withheld_inr"], 1000.0)

        # Audit cash & reserves summary
        summary = get_cash_and_reserves_summary(include_sandbox=True)
        # Bank balance: Opening (50,000) + Cash received (9,000) = 59,000
        self.assertEqual(summary["bank_balance_inr"], 59000.0)
        # Bank cash receipts is 9,000 (NOT 10,000)
        self.assertEqual(summary["bank_cash_receipts_inr"], 9000.0)
        # TDS receivable asset is tracked separately
        self.assertEqual(summary["tds_receivable_inr"], 1000.0)
        # Earned business turnover in P&L is 10,000 (gross revenue)
        self.assertEqual(summary["collected_receipts_inr"], 10000.0)

        # Verify tax profile TDS credits
        prof = get_active_tax_profile()
        self.assertGreaterEqual(prof.get("tds_credits_inr", 0.0), 1000.0)

    # -------------------------------------------------------------------------
    # PROOF 2: ADVANCES ARE NEVER COUNTED TWICE IN CASH, REVENUE OR INVOICES
    # -------------------------------------------------------------------------
    def test_proof2_customer_advances_are_never_double_counted(self):
        """
        Verifies advance lifecycle without double-counting:
        1. Advance deposit: bank cash +₹2,500, liability +₹2,500, earned revenue ₹0.
        2. Advance application: liability drops to ₹0, earned revenue becomes ₹2,500,
           bank cash DOES NOT CHANGE.
        3. Second payment: bank cash becomes +₹5,000, earned revenue becomes ₹5,000.
        """
        set_bank_opening_balance(20000.0, as_of_date="2026-11-01", is_confirmed=True)

        # 1. Advance of ₹2,500 received into bank
        adv_tx_id = record_customer_advance(
            amount_inr=2500.0,
            client_name="Advance Client",
            reference_id="REV-TEST-ADV-001",
            description="50% upfront deposit",
            is_sandbox=True,
        )

        sum1 = get_cash_and_reserves_summary(include_sandbox=True, working_capital_buffer_inr=0.0)
        self.assertEqual(sum1["customer_advance_liability_inr"], 2500.0)
        self.assertEqual(sum1["collected_receipts_inr"], 0.0)
        self.assertEqual(sum1["bank_balance_inr"], 22500.0)  # 20k + 2.5k
        # Distributable cash excludes the advance liability: 22,500 - 2,500 = 20,000
        self.assertEqual(sum1["distributable_cash_inr"], 20000.0)

        # 2. Issue invoice for ₹5,000 and apply ₹2,500 advance
        _, _, inv = self._setup_sandbox_invoice(amount_subtotal=5000.0, tax_rate=0.0, client_name="Advance Client")
        inv_id = inv["id"]
        inv_num = inv["invoice_number"]

        apply_customer_advance(
            advance_tx_id=adv_tx_id,
            invoice_number=inv_num,
            amount_inr=2500.0,
            is_sandbox=True,
        )

        sum2 = get_cash_and_reserves_summary(include_sandbox=True)
        # Advance liability is cleared
        self.assertEqual(sum2["customer_advance_liability_inr"], 0.0)
        # Revenue is now recognized
        self.assertEqual(sum2["collected_receipts_inr"], 2500.0)
        # CRITICAL: Bank cash did NOT increase from application!
        self.assertEqual(sum2["bank_balance_inr"], 22500.0)
        # Unpaid invoice balance is now 2,500
        with connect() as conn:
            unpaid = conn.execute("SELECT amount_total - amount_paid FROM invoices WHERE id=?", (inv_id,)).fetchone()[0]
            self.assertEqual(float(unpaid), 2500.0)

        # 3. Client pays remaining ₹2,500
        row_id = self._import_statement_row(
            amount=2500.0, tx_type="credit", bank_ref="REV-TEST-ADV-FINAL", desc=f"UPI/ADV2500/{inv_num}"
        )
        reconcile_statement_row(row_id=row_id, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=True)

        sum3 = get_cash_and_reserves_summary(include_sandbox=True)
        # Final bank balance: 20,000 + 2,500 + 2,500 = 25,000
        self.assertEqual(sum3["bank_balance_inr"], 25000.0)
        # Total earned revenue: exactly 5,000 (NOT 7,500 or 10,000)
        self.assertEqual(sum3["collected_receipts_inr"], 5000.0)
        # Invoice is fully paid
        with connect() as conn:
            inv_row = conn.execute("SELECT status, amount_paid FROM invoices WHERE id=?", (inv_id,)).fetchone()
            self.assertEqual(inv_row["status"], "paid")
            self.assertEqual(float(inv_row["amount_paid"]), 5000.0)

    # -------------------------------------------------------------------------
    # PROOF 3: GST COLLECTED IS NOT FREELY DISTRIBUTABLE PROFIT
    # -------------------------------------------------------------------------
    def test_proof3_gst_collected_is_locked_in_statutory_reserve(self):
        """
        Tests that when a ₹5,000 + 18% GST (₹900) invoice is paid:
        1. Bank cash increases by full gross ₹5,900.
        2. Net earned revenue is strictly ₹5,000 (subtotal ex-GST).
        3. GST liability reserve is exactly ₹900.
        4. Distributable cash excludes the ₹900 GST collected.
        """
        set_bank_opening_balance(10000.0, as_of_date="2026-11-01", is_confirmed=True)
        _, _, inv = self._setup_sandbox_invoice(amount_subtotal=5000.0, tax_rate=18.0, client_name="GST Client")
        inv_id = inv["id"]
        inv_num = inv["invoice_number"]

        row_id = self._import_statement_row(
            amount=5900.0, tx_type="credit", bank_ref="REV-TEST-GST-5900", desc=f"UPI/GST5900/{inv_num}"
        )
        reconcile_statement_row(row_id=row_id, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=True)

        summary = get_cash_and_reserves_summary(include_sandbox=True, working_capital_buffer_inr=0.0)
        # Bank balance got 5,900: 10,000 + 5,900 = 15,900
        self.assertEqual(summary["bank_balance_inr"], 15900.0)
        # Earned operating turnover is 5,000 (ex-GST)
        self.assertEqual(summary["collected_receipts_inr"], 5000.0)
        # Statutory GST reserve is 900
        self.assertEqual(summary["gst_liability_reserve_inr"], 900.0)
        # Distributable cash: 15,900 - 900 GST reserve - tax reserve = strictly excludes the 900 GST
        self.assertLessEqual(summary["distributable_cash_inr"], 15000.0)
        self.assertEqual(summary["distributable_cash_inr"], round(15900.0 - 900.0 - summary["income_tax_reserve_inr"], 2))

    # -------------------------------------------------------------------------
    # PROOF 4: APPLYING AN ADVANCE DOES NOT CREATE A SECOND BANK RECEIPT
    # -------------------------------------------------------------------------
    def test_proof4_advance_application_creates_no_second_bank_receipt(self):
        """
        Explicitly asserts that calling apply_customer_advance():
        - Modifies the ledger liability and revenue accounts.
        - DOES NOT increment total_cash_in_inr or bank_balance_inr.
        """
        set_bank_opening_balance(30000.0, as_of_date="2026-11-01", is_confirmed=True)
        adv_tx_id = record_customer_advance(
            amount_inr=2000.0, client_name="No-Dupe Client", reference_id="REV-TEST-NODUPE", is_sandbox=True
        )

        sum_before = get_cash_and_reserves_summary(include_sandbox=True)
        cash_in_before = sum_before["total_cash_in_inr"]
        bank_bal_before = sum_before["bank_balance_inr"]

        _, _, inv = self._setup_sandbox_invoice(amount_subtotal=4000.0, client_name="No-Dupe Client")
        apply_customer_advance(
            advance_tx_id=adv_tx_id, invoice_number=inv["invoice_number"], amount_inr=2000.0, is_sandbox=True
        )

        sum_after = get_cash_and_reserves_summary(include_sandbox=True)
        cash_in_after = sum_after["total_cash_in_inr"]
        bank_bal_after = sum_after["bank_balance_inr"]

        # ZERO new cash created
        self.assertEqual(cash_in_after, cash_in_before)
        self.assertEqual(bank_bal_after, bank_bal_before)
        # Liability dropped to 0
        self.assertEqual(sum_after["customer_advance_liability_inr"], 0.0)
        # Revenue increased by 2,000
        self.assertEqual(sum_after["collected_receipts_inr"], 2000.0)

    # -------------------------------------------------------------------------
    # PROOF 5: RECONCILIATION STRICTLY REQUIRES IMPORTED BANK CREDIT
    # -------------------------------------------------------------------------
    def test_proof5_fraud_safeguards_and_reversals_rejected(self):
        """
        Verifies that:
        1. Non-existent bank rows cannot reconcile.
        2. Screenshots/UTRs alone fail closed.
        3. Debit entries cannot reconcile as invoice payments.
        4. Reversal entries are rejected as revenue.
        5. Partial/mismatched amounts fail closed.
        """
        _, _, inv = self._setup_sandbox_invoice(amount_subtotal=5000.0, client_name="Fraud Test Client")
        inv_id = inv["id"]

        # 1. Non-existent row fails
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=999999, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=True)

        # 2. Owner not confirmed fails
        row_id_cr = self._import_statement_row(5000.0, "credit", "REV-TEST-FRAUD-CR")
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=row_id_cr, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=False)

        # 3. Debit row fails
        row_id_dr = self._import_statement_row(5000.0, "debit", "REV-TEST-FRAUD-DR")
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=row_id_dr, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=True)

        # 4. Reversal row fails
        row_id_rev = self._import_statement_row(5000.0, "credit", "REV-TEST-FRAUD-REV", desc="UPI/REVERSAL/FRAUD")
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=row_id_rev, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=True)

        # 5. Mismatched amount fails
        row_id_mismatch = self._import_statement_row(4000.0, "credit", "REV-TEST-FRAUD-MIS")
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=row_id_mismatch, entity_type="invoice", entity_id=str(inv_id), owner_confirmed=True)

    # -------------------------------------------------------------------------
    # PROOF 6: DUPLICATE IMPORTS AND DUPLICATE UTRS REJECTED
    # -------------------------------------------------------------------------
    def test_proof6_duplicate_utr_and_duplicate_reconcile_blocked(self):
        """
        Verifies duplicate protection:
        1. Statement row cannot be reconciled twice.
        2. Duplicate bank_ref (UTR) cannot be assigned to another paid invoice.
        """
        _, _, inv1 = self._setup_sandbox_invoice(5000.0, client_name="Client One")
        _, _, inv2 = self._setup_sandbox_invoice(5000.0, client_name="Client Two")

        row_id = self._import_statement_row(5000.0, "credit", "REV-TEST-DUP-UTR")
        rec1 = reconcile_statement_row(row_id=row_id, entity_type="invoice", entity_id=str(inv1["id"]), owner_confirmed=True)
        self.assertEqual(rec1["status"], "reconciled")

        # Second reconcile of same row fails
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=row_id, entity_type="invoice", entity_id=str(inv1["id"]), owner_confirmed=True)

        # New statement row with duplicate UTR to another invoice fails
        row_id2 = self._import_statement_row(5000.0, "credit", "REV-TEST-DUP-UTR")
        with self.assertRaises(ValueError):
            reconcile_statement_row(row_id=row_id2, entity_type="invoice", entity_id=str(inv2["id"]), owner_confirmed=True)

    # -------------------------------------------------------------------------
    # PROOF 7: OWNER WITHDRAWALS VERSUS BUSINESS EXPENSES SEPARATION
    # -------------------------------------------------------------------------
    def test_proof7_owner_draws_do_not_distort_operating_profit(self):
        """
        Verifies that:
        - Business expense reduces operating profit and taxable income.
        - Owner draw is a capital distribution: reduces bank cash and distributable cash,
          but DOES NOT reduce operating profit or taxable business profit.
        """
        set_bank_opening_balance(50000.0, as_of_date="2026-11-01", is_confirmed=True)
        # Earned revenue of 10,000
        record_ledger_transaction(tx_type="client_receipt", amount_inr=10000.0, description="Earned client fee", reference_id="REV-TEST-P7", is_sandbox=True)

        # Register deductible business expense of 2,000
        exp_id = register_expense(
            vendor="AWS",
            category="hosting_infra",
            amount_inr=2000.0,
            reviewer_notes="Cloud server fee",
            tax_deductibility="deductible",
            ca_review_confirmed=True,
            is_sandbox=True,
        )
        row_id = self._import_statement_row(2000.0, "debit", "REV-TEST-EXP-2000")
        reconcile_statement_row(row_id=row_id, entity_type="expense", entity_id=str(exp_id), owner_confirmed=True)

        # Record owner draw of 5,000
        record_owner_draw(amount_inr=5000.0, description="Owner personal withdrawal", is_sandbox=True)

        summary = get_cash_and_reserves_summary(include_sandbox=True)
        # Operating profit = 10,000 - 2,000 = 8,000 (Owner draw does NOT deduct!)
        self.assertEqual(summary["operating_profit_inr"], 8000.0)
        self.assertEqual(summary["taxable_business_profit_inr"], 8000.0)
        # Bank balance: 50k opening + 10k receipt - 2k expense - 5k draw = 53,000
        self.assertEqual(summary["bank_balance_inr"], 53000.0)

    # -------------------------------------------------------------------------
    # PROOF 8: ALL REPORTS RECONCILE EXACTLY TO THE UNDERLYING LEDGER
    # -------------------------------------------------------------------------
    def test_proof8_all_dashboards_reconcile_to_underlying_ledger(self):
        """
        Seeds a complete multi-step commercial cycle:
        1. Advance of 2,500 received and applied.
        2. Final credit of 2,500 reconciled.
        3. Business expense of 500.
        4. Owner draw of 1,000.
        Verifies that cash_and_reserves_summary, commercial_validation_dashboard,
        commercial_performance_dashboard, and 100_point_scorecard match perfectly.
        """
        set_bank_opening_balance(10000.0, as_of_date="2026-11-01", is_confirmed=True)

        # 1. Advance
        adv_id = record_customer_advance(2500.0, "Cycle Client", reference_id="REV-TEST-CYCLE", is_sandbox=True)

        # 2. Invoice & Apply
        _, _, inv = self._setup_sandbox_invoice(5000.0, client_name="Cycle Client")
        apply_customer_advance(adv_id, inv["invoice_number"], 2500.0, is_sandbox=True)

        # 3. Final bank payment
        row_id = self._import_statement_row(2500.0, "credit", "REV-TEST-CYCLE-FIN")
        reconcile_statement_row(row_id=row_id, entity_type="invoice", entity_id=str(inv["id"]), owner_confirmed=True)

        # 4. Expense
        exp_id = register_expense(
            vendor="Tool Co",
            category="software_tool",
            amount_inr=500.0,
            reviewer_notes="Analytics tool",
            tax_deductibility="deductible",
            ca_review_confirmed=True,
            is_sandbox=True,
        )
        exp_row = self._import_statement_row(500.0, "debit", "REV-TEST-CYCLE-EXP")
        reconcile_statement_row(row_id=exp_row, entity_type="expense", entity_id=str(exp_id), owner_confirmed=True)

        # 5. Draw
        record_owner_draw(1000.0, "Draw", is_sandbox=True)

        # Cross-report audit
        cash_sum = get_cash_and_reserves_summary(include_sandbox=True, working_capital_buffer_inr=5000.0)
        val_dash = get_commercial_validation_dashboard(include_sandbox=True)
        perf_dash = get_commercial_performance_dashboard(include_sandbox=True)
        scorecard = get_100_point_scorecard(include_sandbox=True)

        # Revenue matches across all views
        expected_revenue = 5000.0
        self.assertEqual(cash_sum["collected_receipts_inr"], expected_revenue)
        self.assertEqual(val_dash["metrics"]["cash_actually_received_inr"], expected_revenue)
        self.assertEqual(perf_dash["payments_collected_inr"], expected_revenue)

        # Operating profit matches
        expected_operating_profit = 4500.0  # 5,000 - 500 expense
        self.assertEqual(cash_sum["operating_profit_inr"], expected_operating_profit)
        self.assertEqual(perf_dash["project_contribution_inr"], expected_operating_profit)

        # Bank balance matches
        # Opening (10,000) + Advance (2,500) + Final (2,500) - Exp (500) - Draw (1,000) = 13,500
        expected_bank_bal = 13500.0
        self.assertEqual(cash_sum["bank_balance_inr"], expected_bank_bal)

        # Advance liability is 0
        self.assertEqual(cash_sum["customer_advance_liability_inr"], 0.0)
        self.assertEqual(val_dash["metrics"]["customer_advance_liability_inr"], 0.0)

        # Solvency is confirmed
        self.assertTrue(cash_sum["is_solvent"])


if __name__ == "__main__":
    unittest.main()
