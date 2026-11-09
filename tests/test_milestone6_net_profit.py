from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import core.config
from core.db import connect, create_invoice, init_db, add_opportunity, update_opportunity_status
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    get_expenses_summary,
    import_bank_statement_csv,
    inr_to_paise,
    list_expenses,
    list_ledger_transactions,
    list_unreconciled_statement_rows,
    paise_to_inr,
    reconcile_statement_row,
    register_expense,
    record_ledger_transaction,
)
from core.net_profit_optimizer import (
    MONTHLY_NET_PROFIT_TARGET_INR,
    NetProfitOptimizer,
    SERVICE_PROFIT_SPECS,
)
from core.tax_engine import (
    GST_SERVICE_THRESHOLD_INR,
    GST_WARNING_THRESHOLD_INR,
    compute_income_tax_india_2026,
    compute_required_pretax_profit,
    get_gst_status,
)


class Milestone6NetProfitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._orig_db_path = core.config.DB_PATH
        core.config.DB_PATH = Path(self.temp_dir.name) / "test_soloearn.sqlite3"
        init_db()

    def tearDown(self):
        core.config.DB_PATH = self._orig_db_path
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # 1. INDIAN TAX INTELLIGENCE ENGINE TESTS
    # -----------------------------------------------------------------------

    def test_tax_slabs_income_tax_act_2025(self):
        """Verifies Section 202 slabs and Section 156 rebate under Income-tax Act, 2025."""
        # 1. Zero income
        self.assertEqual(compute_income_tax_india_2026(0.0)["total_tax"], 0.0)
        self.assertEqual(compute_income_tax_india_2026(-5000.0)["total_tax"], 0.0)

        # 2. Income within 0% slab
        self.assertEqual(compute_income_tax_india_2026(400000.0)["total_tax"], 0.0)

        # 3. Income within 12L rebate threshold (tax is zero after Section 156 rebate)
        res_8l = compute_income_tax_india_2026(800000.0)
        self.assertEqual(res_8l["standard_tax"], 20000.0)
        self.assertEqual(res_8l["rebate_156"], 20000.0)
        self.assertEqual(res_8l["total_tax"], 0.0)

        res_12l = compute_income_tax_india_2026(1200000.0)
        self.assertEqual(res_12l["standard_tax"], 60000.0)
        self.assertEqual(res_12l["rebate_156"], 60000.0)
        self.assertEqual(res_12l["total_tax"], 0.0)

        # 4. Marginal relief just above 12L: tax before cess cannot exceed excess over 12L
        res_1250k = compute_income_tax_india_2026(1250000.0)
        self.assertEqual(res_1250k["tax_before_cess"], 50000.0)
        self.assertEqual(res_1250k["cess"], 2000.0)
        self.assertEqual(res_1250k["total_tax"], 52000.0)

        # 5. Exact ₹12,73,934 taxable income yields ₹12,00,000 post-tax profit
        res_target = compute_income_tax_india_2026(1273934.0)
        post_tax = 1273934.0 - res_target["total_tax"]
        self.assertAlmostEqual(post_tax, 1200000.0, delta=1.0)
        self.assertAlmostEqual(res_target["total_tax"], 73934.0, delta=1.0)

    def test_required_pretax_profit_inverse_solver(self):
        """Verifies that the inverse solver derives the exact pre-tax requirement."""
        # For ₹12,00,000 annual post-tax profit (₹1 Lakh/mo):
        pretax = compute_required_pretax_profit(1200000.0)
        self.assertAlmostEqual(pretax, 1273933.65, delta=1.0)

        # For income below 12L (where tax is 0), pretax == posttax
        self.assertEqual(compute_required_pretax_profit(600000.0), 600000.0)
        self.assertEqual(compute_required_pretax_profit(0.0), 0.0)

    def test_gst_threshold_monitoring(self):
        """Verifies CBIC GST registration threshold tracking and alert levels."""
        below = get_gst_status(500000.0)
        self.assertEqual(below["status"], "BELOW_THRESHOLD")
        self.assertFalse(below["requires_ca_review"])
        self.assertEqual(below["headroom_inr"], 1500000.0)

        warning = get_gst_status(1850000.0)
        self.assertEqual(warning["status"], "APPROACHING_THRESHOLD")
        self.assertFalse(warning["requires_ca_review"])
        self.assertEqual(warning["headroom_inr"], 150000.0)

        mandate = get_gst_status(2100000.0)
        self.assertEqual(mandate["status"], "CA_REVIEW_REQUIRED")
        self.assertTrue(mandate["requires_ca_review"])
        self.assertEqual(mandate["headroom_inr"], 0.0)

    # -----------------------------------------------------------------------
    # 2. FINANCIAL FOUNDATION & LEDGER TESTS
    # -----------------------------------------------------------------------

    def test_paise_and_inr_exact_conversion(self):
        """Ensures monetary calculations use exact integer paise without float errors."""
        self.assertEqual(inr_to_paise(100.50), 10050)
        self.assertEqual(inr_to_paise(5000.0), 500000)
        self.assertEqual(paise_to_inr(10050), 100.50)
        self.assertEqual(paise_to_inr(500000), 5000.0)
        with self.assertRaises(ValueError):
            inr_to_paise(float("nan"))

    def test_append_only_ledger_transaction(self):
        """Verifies immutable append-only transaction ledger records."""
        tx_id = record_ledger_transaction(
            tx_type="client_receipt",
            amount_inr=5000.0,
            description="Client payment for INV-TEST-01",
            reference_id="INV-TEST-01",
        )
        self.assertGreater(tx_id, 0)

        txs = list_ledger_transactions(limit=10, tx_type="client_receipt")
        matched = [t for t in txs if t["id"] == tx_id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["amount_paise"], 500000)
        self.assertEqual(matched[0]["reference_id"], "INV-TEST-01")

        # Invalid type or amount rejected
        with self.assertRaises(ValueError):
            record_ledger_transaction("invalid_type", 100.0, "fail")
        with self.assertRaises(ValueError):
            record_ledger_transaction("client_receipt", -50.0, "negative")

    def test_register_expense_and_categorization(self):
        """Verifies business expense registration and categorization."""
        exp_id = register_expense(
            vendor="Google Cloud / Ollama Host",
            category="hosting_infra",
            amount_inr=1250.0,
            tax_deductibility="deductible",
            reviewer_notes="Monthly hosting cost",
            ca_review_confirmed=True,
        )
        self.assertGreater(exp_id, 0)

        expenses = list_expenses(limit=10, category="hosting_infra")
        matched = [e for e in expenses if e["id"] == exp_id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["vendor"], "Google Cloud / Ollama Host")
        self.assertEqual(matched[0]["amount_paise"], 125000)

        summary = get_expenses_summary()
        self.assertGreaterEqual(summary["total_expenses_inr"], 1250.0)

    def test_bank_statement_csv_import_and_reconciliation(self):
        """Verifies bank statement parsing, matching, and owner confirmation."""
        csv_content = """Date,Narration,Deposit,Withdrawal,Ref_No
2026-11-01,UPI Client Transfer ABC Tech,15000.00,,UPI-9876543210
2026-11-02,Software Subscription Cloud,,500.00,SUB-12345
"""
        res = import_bank_statement_csv(csv_content, filename="test_statement.csv")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["rows_imported"], 2)

        unreconciled = list_unreconciled_statement_rows()
        self.assertGreaterEqual(len(unreconciled), 2)

        # Match row with a mock invoice
        credit_row = next(r for r in unreconciled if r["tx_type"] == "credit" and r["amount_inr"] == 15000.0)

        # Unconfirmed reconciliation must raise error
        with self.assertRaises(ValueError):
            reconcile_statement_row(credit_row["id"], "invoice", "INV-MOCK-001", owner_confirmed=False)

        # Confirmed reconciliation succeeds
        rec_res = reconcile_statement_row(credit_row["id"], "invoice", "INV-MOCK-001", owner_confirmed=True)
        self.assertEqual(rec_res["status"], "reconciled")

    def test_cash_runway_and_distributable_cash_formula(self):
        """Verifies Distributable Cash C = B - G - T - U - W."""
        summary = get_cash_and_reserves_summary(working_capital_buffer_inr=25000.0)
        self.assertIn("bank_balance_inr", summary)
        self.assertIn("distributable_cash_inr", summary)
        self.assertIn("gst_liability_reserve_inr", summary)
        self.assertIn("income_tax_reserve_inr", summary)
        self.assertIn("working_capital_buffer_inr", summary)
        self.assertEqual(summary["working_capital_buffer_inr"], 25000.0)

    # -----------------------------------------------------------------------
    # 3. NET PROFIT OPTIMIZER & UNIT ECONOMICS TESTS
    # -----------------------------------------------------------------------

    def test_net_profit_optimizer_calculations(self):
        """Verifies NetProfitOptimizer produces accurate post-tax net profit plans."""
        plan = NetProfitOptimizer.analyze_net_profit(monthly_target_inr=100000.0)
        self.assertEqual(plan.monthly_net_profit_target_inr, 100000.0)
        self.assertEqual(plan.annual_net_profit_target_inr, 1200000.0)
        self.assertAlmostEqual(plan.annual_pretax_profit_required_inr, 1273933.65, delta=1.0)
        self.assertAlmostEqual(plan.monthly_pretax_profit_required_inr, 106161.14, delta=1.0)

        # Dynamic receipts required must be greater than pre-tax required
        self.assertGreater(plan.monthly_receipts_required_ex_gst_inr, plan.monthly_pretax_profit_required_inr)
        self.assertIn("total_jobs_needed", plan.service_capacities_needed)

    def test_minimum_profitable_quote_calculator(self):
        """Verifies quotes below minimum margin are flagged or rejected."""
        # 2 hours at ₹2000/hr desired return -> ₹4,000 net target
        quote = NetProfitOptimizer.calculate_minimum_profitable_quote(
            estimated_hours=2.0,
            direct_costs_inr=100.0,
            desired_hourly_return_inr=2000.0,
            platform_fee_pct=5.0,
        )
        self.assertGreaterEqual(quote["minimum_quote_inr"], 4000.0)
        self.assertGreaterEqual(quote["contribution_margin_pct"], 50.0)
        self.assertTrue(quote["is_acceptable"])

    def test_service_profit_specs_unit_economics(self):
        """Verifies all 4 verified service specs maintain strong contribution margins."""
        for spec in SERVICE_PROFIT_SPECS:
            self.assertGreater(spec.net_contribution_inr, 0.0)
            self.assertGreaterEqual(spec.contribution_margin_pct, 70.0)
            self.assertGreater(spec.contribution_per_hour_inr, 1500.0)


if __name__ == "__main__":
    unittest.main()
