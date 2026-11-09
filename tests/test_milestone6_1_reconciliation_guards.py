from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.config
from core.db import (
    add_opportunity,
    connect,
    create_invoice,
    create_task,
    get_invoice,
    init_db,
    set_bank_opening_balance,
    upsert_tax_profile,
    verify_invoice_payment,
)
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    import_bank_statement_csv,
    list_unreconciled_statement_rows,
    record_invoice_refund,
    record_ledger_transaction,
    reconcile_statement_row,
    register_expense,
)
from core.net_profit_optimizer import NetProfitOptimizer
from core.tax_engine import compute_income_tax_india_2026


class Milestone61ReconciliationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._orig_db_path = core.config.DB_PATH
        core.config.DB_PATH = Path(self.temp_dir.name) / "test_m6_1_guards.sqlite3"
        init_db()

    def tearDown(self):
        core.config.DB_PATH = self._orig_db_path
        self.temp_dir.cleanup()

    def _create_invoice(self, amount=1000.0, tax_rate=18.0):
        opp_id = add_opportunity({
            "title": "M6.1 reconciliation test",
            "source": "test",
            "amount_inr": amount,
            "estimated_hours": 2.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 1.0,
            "source_permitted": 1,
            "approval_status": "approved",
            "status": "won",
        }, score=90.0)
        task_id = create_task("Verified test task", "data_analytics", "workspace", opp_id)
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?",
                (task_id,),
            )
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        inv = create_invoice(
            opportunity_id=opp_id,
            client_name="Test Client",
            amount_subtotal=amount,
            tax_rate=tax_rate,
            task_id=task_id,
        )
        return inv

    def test_partial_invoice_credit_cannot_mark_full_invoice_paid(self):
        inv = self._create_invoice()
        import_bank_statement_csv(
            f"Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-09,Partial payment,500.00,,PARTIAL-INV-1\n",
            "partial_invoice.csv",
        )
        row = list_unreconciled_statement_rows()[0]
        with self.assertRaisesRegex(ValueError, "Partial/over-payments are not supported"):
            reconcile_statement_row(row["id"], "invoice", inv["invoice_number"], owner_confirmed=True)

        self.assertEqual(get_invoice(invoice_id=inv["id"])["status"], "issued")
        self.assertEqual(len(list_unreconciled_statement_rows()), 1)
        with connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM financial_transactions WHERE tx_type='client_receipt'"
                ).fetchone()[0],
                0,
            )

    def test_overpayment_cannot_mark_invoice_paid(self):
        inv = self._create_invoice()
        import_bank_statement_csv(
            f"Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-09,Overpayment,1200.00,,OVER-INV-1\n",
            "over_invoice.csv",
        )
        row = list_unreconciled_statement_rows()[0]
        with self.assertRaisesRegex(ValueError, "does not exactly settle invoice"):
            reconcile_statement_row(row["id"], "invoice", inv["invoice_number"], owner_confirmed=True)
        self.assertEqual(get_invoice(invoice_id=inv["id"])["status"], "issued")

    def test_partial_expense_debit_cannot_mark_full_expense_paid(self):
        exp_id = register_expense(
            vendor="Test Hosting",
            category="hosting_infra",
            amount_inr=1000.0,
            expense_date="2026-11-09",
        )
        import_bank_statement_csv(
            "Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-09,Partial expense,,400.00,EXP-PART-1\n",
            "partial_expense.csv",
        )
        row = list_unreconciled_statement_rows()[0]
        with self.assertRaisesRegex(ValueError, "Partial expense payments are not supported"):
            reconcile_statement_row(row["id"], "expense", f"EXP-{exp_id:05d}", owner_confirmed=True)
        self.assertEqual(len(list_unreconciled_statement_rows()), 1)
        with connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM financial_transactions WHERE tx_type='expense'").fetchone()[0],
                0,
            )

    def test_reconciled_statement_row_cannot_be_reused(self):
        inv = self._create_invoice()
        gross = float(inv["amount_total"])
        import_bank_statement_csv(
            f"Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-09,Full payment,{gross:.2f},,ONE-USE-REF\n",
            "full_invoice.csv",
        )
        row = list_unreconciled_statement_rows()[0]
        reconcile_statement_row(row["id"], "invoice", inv["invoice_number"], owner_confirmed=True)
        with self.assertRaisesRegex(ValueError, "already been reconciled"):
            reconcile_statement_row(row["id"], "invoice", inv["invoice_number"], owner_confirmed=True)
        with connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM financial_transactions WHERE tx_type='client_receipt'"
                ).fetchone()[0],
                1,
            )

    def test_invalid_invoice_match_does_not_consume_statement_row(self):
        import_bank_statement_csv(
            "Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-09,Unknown invoice,100.00,,BAD-INVOICE\n",
            "invalid_invoice.csv",
        )
        row = list_unreconciled_statement_rows()[0]
        with self.assertRaisesRegex(ValueError, "was not found"):
            reconcile_statement_row(row["id"], "invoice", "INV-2099-9999", owner_confirmed=True)
        self.assertEqual(len(list_unreconciled_statement_rows()), 1)

    def test_other_bank_movement_updates_cash_but_not_profit(self):
        set_bank_opening_balance(5000.0, as_of_date="2026-11-01", is_confirmed=True)
        import_bank_statement_csv(
            "Date,Narration,Deposit,Withdrawal,Ref_No\n2026-11-09,Personal purchase,,250.00,OTHER-DB-1\n",
            "other_debit.csv",
        )
        row = list_unreconciled_statement_rows()[0]
        reconcile_statement_row(row["id"], "other", "personal_purchase", owner_confirmed=True)
        summary = get_cash_and_reserves_summary(month="2026-11")
        self.assertEqual(summary["bank_balance_inr"], 4750.0)
        self.assertEqual(summary["operating_profit_inr"], 0.0)

    def test_invoice_refund_reduces_service_profit_and_gst_reserve_separately(self):
        inv = self._create_invoice()
        verify_invoice_payment(
            invoice_id=inv["id"],
            payment_reference="INV-PAID-REF-1",
            verified_amount=float(inv["amount_total"]),
            owner_confirmation=True,
        )
        refund = record_invoice_refund(
            invoice_number=inv["invoice_number"],
            gross_refund_inr=float(inv["amount_total"]) / 2,
            refund_reference="REFUND-001",
            owner_confirmed=True,
            tx_date="2026-11-09",
        )
        self.assertEqual(refund["service_refund_ex_gst_inr"], 500.0)
        self.assertEqual(refund["gst_refund_inr"], 90.0)
        summary = get_cash_and_reserves_summary(month="2026-11")
        self.assertEqual(summary["collected_receipts_inr"], 500.0)
        self.assertEqual(summary["operating_profit_inr"], 500.0)
        self.assertEqual(summary["gst_liability_reserve_inr"], 90.0)

    def test_duplicate_invoice_refund_reference_rejected(self):
        inv = self._create_invoice()
        verify_invoice_payment(
            invoice_id=inv["id"],
            payment_reference="INV-PAID-REF-2",
            verified_amount=float(inv["amount_total"]),
            owner_confirmation=True,
        )
        kwargs = dict(
            invoice_number=inv["invoice_number"],
            gross_refund_inr=100.0,
            refund_reference="DUP-REFUND",
            owner_confirmed=True,
        )
        record_invoice_refund(**kwargs)
        with self.assertRaisesRegex(ValueError, "already been recorded"):
            record_invoice_refund(**kwargs)

    def test_unsupported_regime_fails_instead_of_using_new_regime(self):
        with self.assertRaisesRegex(ValueError, "currently supports only"):
            compute_income_tax_india_2026(1500000.0, regime="old_1961")
        upsert_tax_profile(regime="old_1961")
        with self.assertRaisesRegex(ValueError, "regime='new_2025'"):
            NetProfitOptimizer.analyze_net_profit()


    def test_mtd_operating_loss_is_not_clamped_to_zero(self):
        record_ledger_transaction(
            "client_receipt", 1000.0, "Small receipt", "INV-LOSS-1", "2026-11-01"
        )
        register_expense(
            vendor="Test Supplier",
            category="hosting_infra",
            amount_inr=2000.0,
            expense_date="2026-11-02",
        )
        summary = get_cash_and_reserves_summary(month="2026-11")
        self.assertEqual(summary["operating_profit_inr"], -1000.0)
        plan = NetProfitOptimizer.analyze_net_profit(monthly_target_inr=100000.0)
        self.assertEqual(plan.mtd_net_profit_after_tax_inr, -1000.0)
        self.assertEqual(plan.net_profit_gap_inr, 101000.0)

    def test_gst_turnover_includes_unpaid_invoices_and_other_same_pan_turnover(self):
        inv = self._create_invoice(amount=600000.0, tax_rate=0.0)
        self.assertEqual(inv["status"], "issued")
        upsert_tax_profile(other_pan_aggregate_turnover_inr=1500000.0)
        plan = NetProfitOptimizer.analyze_net_profit(monthly_target_inr=100000.0)
        self.assertEqual(plan.gst_status["soloearn_invoice_turnover_inr"], 600000.0)
        self.assertEqual(plan.gst_status["other_same_pan_turnover_inr"], 1500000.0)
        self.assertEqual(plan.gst_status["turnover_inr"], 2100000.0)
        self.assertEqual(plan.gst_status["status"], "CA_REVIEW_REQUIRED")

    def test_after_tax_job_count_includes_incremental_tax_not_only_pretax_margin(self):
        pre_tax_simple_count = 10
        count = NetProfitOptimizer._jobs_required_for_after_tax_gap(
            after_tax_gap_inr=100000.0,
            contribution_per_job_inr=10000.0,
            base_taxable_monthly_inr=50000.0,
            other_annual_income_inr=0.0,
            regime="new_2025",
        )
        self.assertGreater(count, pre_tax_simple_count)



if __name__ == "__main__":
    unittest.main()
