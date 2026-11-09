from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import core.config
from core.db import (
    connect,
    create_invoice,
    create_task,
    init_db,
    add_opportunity,
    set_bank_opening_balance,
    get_bank_opening_balance,
    upsert_tax_profile,
    get_active_tax_profile,
    verify_invoice_payment,
)
from core.financial_ledger import (
    confirm_expense_tax_deductibility,
    get_cash_and_reserves_summary,
    get_expenses_summary,
    import_bank_statement_csv,
    inr_to_paise,
    list_expenses,
    list_ledger_transactions,
    list_unreconciled_statement_rows,
    paise_to_inr,
    reconcile_statement_row,
    record_ledger_transaction,
    register_expense,
)
from core.net_profit_optimizer import (
    MONTHLY_NET_PROFIT_TARGET_INR,
    NetProfitOptimizer,
)
from core.tax_engine import (
    compute_business_incremental_tax,
    compute_income_tax_india_2026,
    compute_required_pretax_profit,
    get_gst_status,
)


class Milestone6FinancialIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._orig_db_path = core.config.DB_PATH
        core.config.DB_PATH = Path(self.temp_dir.name) / "test_soloearn.sqlite3"
        init_db()

    def tearDown(self):
        core.config.DB_PATH = self._orig_db_path
        self.temp_dir.cleanup()

    def test_bank_statement_invoice_reconciliation_posts_legacy_and_m6_ledgers_once(self):
        opp_id = add_opportunity({
            "title": "Corporate CSV Pipeline",
            "source": "kaggle",
            "amount_inr": 10000.0,
            "estimated_hours": 3.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 1.0,
            "source_permitted": 1,
            "approval_status": "approved",
            "status": "won",
        }, score=85.0)
        task_id = create_task("Pipeline execution", "data_engineering", "workspace", opp_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task_id,))
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        inv = create_invoice(
            opportunity_id=opp_id,
            client_name="Acme Corp",
            amount_subtotal=10000.0,
            tax_rate=18.0,
            task_id=task_id,
        )
        inv_num = inv["invoice_number"]

        csv_content = f"Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-05,Payment Acme Corp,11800.00,,UPI-{inv_num}\n"
        import_bank_statement_csv(csv_content, filename="stmt.csv")
        unrec = list_unreconciled_statement_rows()
        self.assertEqual(len(unrec), 1)

        res = reconcile_statement_row(unrec[0]["id"], "invoice", inv_num, owner_confirmed=True)
        self.assertEqual(res["status"], "reconciled")

        with connect() as conn:
            inv_row = conn.execute("SELECT status, payment_verified_by_owner FROM invoices WHERE id = ?", (inv["id"],)).fetchone()
            self.assertEqual(inv_row["status"], "paid")
            self.assertEqual(inv_row["payment_verified_by_owner"], 1)

            rev_rows = conn.execute("SELECT * FROM revenue WHERE opportunity_id = ?", (opp_id,)).fetchall()
            self.assertEqual(len(rev_rows), 1)
            self.assertEqual(rev_rows[0]["status"], "paid")

            tx_receipts = conn.execute(
                "SELECT * FROM financial_transactions WHERE reference_id = ? AND tx_type = 'client_receipt'",
                (inv_num,),
            ).fetchall()
            self.assertEqual(len(tx_receipts), 1)
            self.assertEqual(tx_receipts[0]["amount_inr"], 10000.0)

            tx_gst = conn.execute(
                "SELECT * FROM financial_transactions WHERE reference_id = ? AND tx_type = 'gst_collected'",
                (inv_num,),
            ).fetchall()
            self.assertEqual(len(tx_gst), 1)
            self.assertEqual(tx_gst[0]["amount_inr"], 1800.0)

    def test_ca_reviewed_deductible_cost_reduces_taxable_profit(self):
        record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=50000.0,
            description="Client receipt",
            reference_id="INV-001",
            tx_date="2026-11-05",
        )
        register_expense(
            vendor="Hetzner Cloud",
            category="hosting_infra",
            amount_inr=10000.0,
            expense_date="2026-11-06",
            business_use_pct=100.0,
            tax_deductibility="deductible",
            ca_review_confirmed=True,
            ca_review_notes="CA verified hosting bill",
        )

        summary = get_cash_and_reserves_summary(month="2026-11")
        self.assertEqual(summary["collected_receipts_inr"], 50000.0)
        self.assertEqual(summary["total_operating_expenses_inr"], 10000.0)
        self.assertEqual(summary["deductible_operating_expenses_inr"], 10000.0)
        self.assertEqual(summary["operating_profit_inr"], 40000.0)
        self.assertEqual(summary["taxable_business_profit_inr"], 40000.0)

    def test_invoice_owner_confirmed_receipt_posts_once_to_m6_ledger(self):
        opp_id = add_opportunity({
            "title": "Analytics Dashboard",
            "source": "kaggle",
            "amount_inr": 20000.0,
            "estimated_hours": 4.0,
            "skill_match": 0.95,
            "payment_confidence": 0.95,
            "risk_level": 1.0,
            "source_permitted": 1,
            "approval_status": "approved",
            "status": "won",
        }, score=90.0)
        task_id = create_task("Dashboard build", "interactive_dashboard", "workspace", opp_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task_id,))
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        inv = create_invoice(
            opportunity_id=opp_id,
            client_name="Alpha Tech",
            amount_subtotal=20000.0,
            tax_rate=18.0,
            task_id=task_id,
        )
        inv_id = inv["id"]
        inv_num = inv["invoice_number"]

        verify_invoice_payment(
            invoice_id=inv_id,
            payment_reference="REF-AUTOCONFIRM-001",
            verified_amount=float(inv["amount_total"]),
            owner_confirmation=True,
        )

        with connect() as conn:
            txs = conn.execute(
                "SELECT * FROM financial_transactions WHERE reference_id = ? AND tx_type = 'client_receipt'",
                (inv_num,),
            ).fetchall()
            self.assertEqual(len(txs), 1)

    def test_month_summary_excludes_prior_month_ledger_transactions(self):
        record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=25000.0,
            description="October job",
            reference_id="INV-OCT",
            tx_date="2026-10-15",
        )
        record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=45000.0,
            description="November job",
            reference_id="INV-NOV",
            tx_date="2026-11-05",
        )

        register_expense(
            vendor="October Tool",
            category="software_tool",
            amount_inr=5000.0,
            expense_date="2026-10-12",
        )
        register_expense(
            vendor="November Tool",
            category="software_tool",
            amount_inr=7000.0,
            expense_date="2026-11-02",
        )

        summary_nov = get_cash_and_reserves_summary(month="2026-11")
        self.assertEqual(summary_nov["collected_receipts_inr"], 45000.0)
        self.assertEqual(summary_nov["total_operating_expenses_inr"], 7000.0)
        self.assertEqual(summary_nov["operating_profit_inr"], 38000.0)

    def test_opening_balance_required_before_claiming_actual_bank_balance(self):
        summary_unconfirmed = get_cash_and_reserves_summary()
        self.assertFalse(summary_unconfirmed["bank_balance_known"])
        self.assertIsNone(summary_unconfirmed["bank_balance_inr"])
        self.assertIsNone(summary_unconfirmed["distributable_cash_inr"])

        set_bank_opening_balance(75000.0, as_of_date="2026-11-01", is_confirmed=True)

        summary_confirmed = get_cash_and_reserves_summary()
        self.assertTrue(summary_confirmed["bank_balance_known"])
        self.assertEqual(summary_confirmed["bank_balance_inr"], 75000.0)
        self.assertIsNotNone(summary_confirmed["distributable_cash_inr"])

    def test_optimizer_handles_goal_reached_when_bank_balance_unknown(self):
        record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=200000.0,
            description="Large enterprise delivery",
            reference_id="INV-BIG",
            tx_date="2026-11-02",
        )
        plan = NetProfitOptimizer.analyze_net_profit(monthly_target_inr=100000.0)
        self.assertGreaterEqual(plan.mtd_net_profit_after_tax_inr, 100000.0)
        self.assertEqual(plan.net_profit_gap_inr, 0.0)
        self.assertIsNone(plan.distributable_cash_inr)
        self.assertFalse(plan.bank_balance_known)
        self.assertIn("unknown (opening balance unconfirmed)", plan.recommended_action)

    def test_optimizer_uses_other_income_in_tax_profile(self):
        upsert_tax_profile(other_annual_income_inr=600000.0)
        profile = get_active_tax_profile()
        self.assertIsNotNone(profile)
        self.assertEqual(profile["other_annual_income_inr"], 600000.0)

        plan = NetProfitOptimizer.analyze_net_profit(monthly_target_inr=100000.0)
        self.assertGreater(plan.annual_pretax_profit_required_inr, 1273933.65)

    def test_paid_expense_is_counted_as_cash_outflow_once_when_reconciled(self):
        set_bank_opening_balance(100000.0, as_of_date="2026-11-01", is_confirmed=True)
        exp_id = register_expense(
            vendor="Server Provider",
            category="hosting_infra",
            amount_inr=15000.0,
            expense_date="2026-11-02",
        )

        csv_content = f"Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-03,Payment Server Provider,,15000.00,DEB-001\n"
        import_bank_statement_csv(csv_content, filename="stmt.csv")
        unrec = list_unreconciled_statement_rows()
        self.assertEqual(len(unrec), 1)

        reconcile_statement_row(unrec[0]["id"], "expense", str(exp_id), owner_confirmed=True)

        summary = get_cash_and_reserves_summary(working_capital_buffer_inr=0.0)
        self.assertEqual(summary["bank_balance_inr"], 85000.0)
        self.assertEqual(summary["unpaid_bills_inr"], 0.0)
        self.assertEqual(summary["distributable_cash_inr"], 85000.0)

    def test_required_receipts_solver_accounts_for_unreviewed_non_deductible_costs(self):
        receipts_deductible = NetProfitOptimizer.solve_required_receipts(
            monthly_target=100000.0,
            cost_ratio=0.25,
            deductible_ratio=0.25,
            other_annual_income=0.0,
        )
        receipts_nondeductible = NetProfitOptimizer.solve_required_receipts(
            monthly_target=100000.0,
            cost_ratio=0.25,
            deductible_ratio=0.0,
            other_annual_income=0.0,
        )
        self.assertGreater(receipts_nondeductible, receipts_deductible)

    def test_tax_deduction_cannot_be_marked_without_review_confirmation(self):
        with self.assertRaises(ValueError):
            register_expense(
                vendor="Test SaaS",
                category="software_tool",
                amount_inr=2000.0,
                tax_deductibility="deductible",
                ca_review_confirmed=False,
            )

        with self.assertRaises(ValueError):
            register_expense(
                vendor="Test SaaS",
                category="software_tool",
                amount_inr=2000.0,
                tax_deductibility="partly_deductible",
                ca_review_confirmed=False,
            )

        exp_id = register_expense(
            vendor="Test SaaS",
            category="software_tool",
            amount_inr=2000.0,
            tax_deductibility="unknown",
            ca_review_confirmed=False,
        )

        with self.assertRaises(ValueError):
            confirm_expense_tax_deductibility(exp_id, "deductible", ca_review_confirmed=False)

        res = confirm_expense_tax_deductibility(
            exp_id,
            "deductible",
            ca_review_confirmed=True,
            ca_review_notes="CA verified voucher",
            reviewed_by="Proprietor with CA",
        )
        self.assertEqual(res["status"], "updated")

    def test_tax_engine_applies_surcharge_and_marginal_relief(self):
        # 51L: standard slab tax 11,10,000; surcharge capped by 10,80,000 + 1L - 11,10,000 = 70,000
        t51 = compute_income_tax_india_2026(5100000.0)
        self.assertEqual(t51["surcharge_rate_pct"], 10.0)
        self.assertEqual(t51["tax_before_cess"], 1180000.0)
        self.assertGreater(t51["marginal_relief_surcharge"], 0.0)

        # 1.01Cr: standard slab tax 26,10,000; surcharge capped by 28,38,000 + 1L - 26,10,000 = 3,28,000
        t101 = compute_income_tax_india_2026(10100000.0)
        self.assertEqual(t101["surcharge_rate_pct"], 15.0)
        self.assertEqual(t101["tax_before_cess"], 2938000.0)
        self.assertGreater(t101["marginal_relief_surcharge"], 0.0)

        # 2.01Cr: standard slab tax 56,10,000; surcharge capped by 64,17,000 + 1L - 56,10,000 = 9,07,000
        t201 = compute_income_tax_india_2026(20100000.0)
        self.assertEqual(t201["surcharge_rate_pct"], 25.0)
        self.assertEqual(t201["tax_before_cess"], 6517000.0)
        self.assertGreater(t201["marginal_relief_surcharge"], 0.0)

    def test_tax_profile_other_income_increases_required_business_profit(self):
        pretax_zero = compute_required_pretax_profit(1200000.0, other_annual_income=0.0)
        pretax_with_other = compute_required_pretax_profit(1200000.0, other_annual_income=800000.0)
        self.assertGreater(pretax_with_other, pretax_zero)

    def test_unknown_tax_deductibility_is_not_deducted_in_taxable_profit(self):
        record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=60000.0,
            description="Client receipt",
            reference_id="INV-002",
            tx_date="2026-11-04",
        )
        register_expense(
            vendor="Mystery Expense",
            category="other",
            amount_inr=15000.0,
            expense_date="2026-11-05",
            tax_deductibility="unknown",
            ca_review_confirmed=False,
        )

        summary = get_cash_and_reserves_summary(month="2026-11")
        self.assertEqual(summary["total_operating_expenses_inr"], 15000.0)
        self.assertEqual(summary["deductible_operating_expenses_inr"], 0.0)
        self.assertEqual(summary["operating_profit_inr"], 45000.0)
        self.assertEqual(summary["taxable_business_profit_inr"], 60000.0)

    def test_unpaid_expense_is_not_subtracted_from_bank_cash_then_subtracted_again(self):
        set_bank_opening_balance(100000.0, as_of_date="2026-11-01", is_confirmed=True)
        register_expense(
            vendor="Software Tool Incurred",
            category="software_tool",
            amount_inr=12000.0,
            expense_date="2026-11-02",
        )

        summary = get_cash_and_reserves_summary(working_capital_buffer_inr=0.0)
        # Bank balance should remain 100,000 (cash hasn't left bank yet)
        self.assertEqual(summary["bank_balance_inr"], 100000.0)
        # Unpaid bills should capture the 12,000
        self.assertEqual(summary["unpaid_bills_inr"], 12000.0)
        # Distributable cash C = 100,000 - 12,000 = 88,000 (subtracted exactly once)
        self.assertEqual(summary["distributable_cash_inr"], 88000.0)

    def test_vendor_gst_is_not_recorded_as_gst_payment_or_automatic_itc(self):
        register_expense(
            vendor="Equipment Supplier",
            category="office_overhead",
            amount_inr=5000.0,
            gst_amount_inr=900.0,
            expense_date="2026-11-01",
        )

        with connect() as conn:
            gst_txs = conn.execute(
                "SELECT * FROM financial_transactions WHERE tx_type = 'gst_paid'"
            ).fetchall()
            self.assertEqual(len(gst_txs), 0)

        summary = get_cash_and_reserves_summary()
        self.assertEqual(summary["gst_liability_reserve_inr"], 0.0)


if __name__ == "__main__":
    unittest.main()
