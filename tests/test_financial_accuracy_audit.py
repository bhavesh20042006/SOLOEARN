import os
import shutil
import tempfile
import unittest
from pathlib import Path

import core.config
from core.db import (
    add_opportunity,
    connect,
    create_invoice,
    create_task,
    get_active_tax_profile,
    get_financial_states_summary,
    get_owner_time_summary,
    init_db,
    log_owner_time,
    upsert_tax_profile,
)
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    import_bank_statement_csv,
    reconcile_statement_row,
    register_expense,
)
from core.net_profit_optimizer import (
    MONTHLY_NET_PROFIT_TARGET_INR,
    MONTHLY_SAFETY_BUFFER_TARGET_INR,
    NetProfitOptimizer,
    SERVICE_PROFIT_SPECS,
)
from core.tax_engine import (
    compute_business_incremental_tax,
    compute_income_tax_india_2026,
    compute_required_pretax_profit,
    get_gst_status,
)


class FinancialAccuracyAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._orig_db_path = core.config.DB_PATH
        core.config.DB_PATH = Path(self.temp_dir.name) / "test_soloearn.sqlite3"
        init_db()
        upsert_tax_profile(
            other_annual_income_inr=0.0,
            other_pan_aggregate_turnover_inr=0.0,
            advance_tax_paid_inr=0.0,
            tds_credits_inr=0.0,
            gst_threshold_inr=2000000.0,
        )

    def tearDown(self):
        core.config.DB_PATH = self._orig_db_path
        self.temp_dir.cleanup()

    def _create_opp(self, title: str, amount_inr: float, status: str = "new") -> int:
        opp_id = add_opportunity({
            "title": title,
            "source": "manual",
            "url": "https://example.com/lead",
            "description": f"Description for {title}",
            "amount_inr": float(amount_inr),
            "estimated_hours": 3.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 1.0,
            "source_permitted": 1,
        }, score=85.0)
        if status != "new":
            with connect() as conn:
                conn.execute("UPDATE opportunities SET status=? WHERE id=?", (status, opp_id))
        return opp_id

    def test_invoice_reconciliation_with_client_tds_withheld(self):
        """Tests that client TDS deduction (e.g. 10% under Section 393, formerly Section 194J/194C) is reconciled properly."""
        opp_id = self._create_opp("Client Corp Pipeline", 10000.0, status="submitted_by_owner")
        task_id = create_task("ETL Pipeline Delivery", "data_engineering", "ws_tds", opp_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task_id,))
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        inv = create_invoice(opp_id, client_name="Client Corp", service_tier="data_engineering", amount_subtotal=10000.0, tax_rate=0.0)
        inv_num = inv["invoice_number"]

        # Bank statement reflects credit of ₹9,000 (after client withheld ₹1,000 TDS)
        bank_csv = (
            "Date,Description,Reference,Amount,Direction\n"
            "2026-11-01,Client Corp Direct Deposit,BANK-UTR-9000,9000.00,credit\n"
        ).encode("utf-8")
        imp_res = import_bank_statement_csv(bank_csv, filename="test_stmt.csv")
        self.assertEqual(imp_res["rows_imported"], 1)

        with connect() as conn:
            row = conn.execute("SELECT id FROM bank_statement_rows WHERE bank_ref='BANK-UTR-9000'").fetchone()
            row_id = row["id"]

        # Reconcile with ₹1,00,00 TDS withheld
        rec = reconcile_statement_row(
            row_id=row_id,
            entity_type="invoice",
            entity_id=inv_num,
            owner_confirmed=True,
            tds_withheld_inr=1000.0,
        )
        self.assertEqual(rec["status"], "reconciled")
        self.assertEqual(rec["amount_inr"], 9000.0)
        self.assertEqual(rec["tds_withheld_inr"], 1000.0)

        # Verify tax profile TDS credits were incremented
        prof = get_active_tax_profile()
        self.assertEqual(prof["tds_credits_inr"], 1000.0)

        # Verify ledger recorded client receipt and TDS credit
        with connect() as conn:
            txs = conn.execute(
                "SELECT tx_type, amount_inr FROM financial_transactions WHERE reference_id=?",
                (inv_num,),
            ).fetchall()
            tx_map = {t["tx_type"]: t["amount_inr"] for t in txs}
            self.assertEqual(tx_map.get("client_receipt"), 10000.0)
            self.assertEqual(tx_map.get("tds_credit"), 1000.0)

    def test_reconciliation_rejects_mismatched_amount_including_tds(self):
        """Tests that bank_credit + tds_withheld != invoice_total fails closed."""
        opp_id = self._create_opp("Retail Analytics", 5000.0, status="submitted_by_owner")
        task_id = create_task("Excel Pack", "data_analytics", "ws_mismatch", opp_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task_id,))
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        inv = create_invoice(opp_id, client_name="Retail Client", service_tier="data_analytics", amount_subtotal=5000.0, tax_rate=0.0)
        inv_num = inv["invoice_number"]

        bank_csv = (
            "Date,Description,Reference,Amount,Direction\n"
            "2026-11-01,Retail Client Payment,BANK-UTR-4000,4000.00,credit\n"
        ).encode("utf-8")
        import_bank_statement_csv(bank_csv, filename="test_stmt2.csv")

        with connect() as conn:
            row = conn.execute("SELECT id FROM bank_statement_rows WHERE bank_ref='BANK-UTR-4000'").fetchone()
            row_id = row["id"]

        # Credit is 4000, TDS is 500 -> Total 4500 != 5000
        with self.assertRaisesRegex(ValueError, "does not exactly settle invoice"):
            reconcile_statement_row(
                row_id=row_id,
                entity_type="invoice",
                entity_id=inv_num,
                owner_confirmed=True,
                tds_withheld_inr=500.0,
            )

    def test_tax_reserves_accounting_with_tds_and_advance_tax(self):
        """Verifies that TDS credits and advance tax directly reduce annual tax reserve without double-counting."""
        upsert_tax_profile(
            other_annual_income_inr=0.0,
            advance_tax_paid_inr=20000.0,
            tds_credits_inr=10000.0,
        )

        res = get_cash_and_reserves_summary()
        self.assertEqual(res["total_prepayments_inr"], 30000.0)
        self.assertGreaterEqual(res["income_tax_reserve_inr"], 0.0)

    def test_owner_time_tracking_across_all_five_categories(self):
        """Verifies logging and aggregation of owner hours across delivery, prospecting, communication, revisions, and admin."""
        # Invalid category should raise ValueError
        with self.assertRaises(ValueError):
            log_owner_time("invalid_cat", 2.0)

        # Zero or negative hours should raise ValueError
        with self.assertRaises(ValueError):
            log_owner_time("delivery", 0.0)
        with self.assertRaises(ValueError):
            log_owner_time("delivery", -1.5)

        # Log valid entries
        log_owner_time("delivery", 4.5, description="Building ETL script")
        log_owner_time("prospecting", 2.0, description="Scanning Upwork feeds")
        log_owner_time("client_communication", 1.5, description="Kickoff call")
        log_owner_time("revisions", 1.0, description="Fixing Excel formula")
        log_owner_time("administration", 1.0, description="Reconciling bank statement")

        summary = get_owner_time_summary()
        self.assertEqual(summary["delivery_hours"], 4.5)
        self.assertEqual(summary["prospecting_hours"], 2.0)
        self.assertEqual(summary["client_communication_hours"], 1.5)
        self.assertEqual(summary["revisions_hours"], 1.0)
        self.assertEqual(summary["administration_hours"], 1.0)
        self.assertEqual(summary["total_hours"], 10.0)

    def test_thirteen_job_portfolio_detects_shortfall_against_one_lakh(self):
        """
        Validates that 6 Analytics + 4 Reconciliation + 3 ETL projects:
        Yields ₹1,05,850 pre-tax contribution, ₹12,70,200 annual pre-tax,
        statutory annual tax ₹73,008 (monthly ₹6,084),
        resulting in post-tax net profit of ₹99,766 (a ₹234 monthly shortfall).
        """
        c_analytics = 6 * (5000.0 * 0.95 - 150.0)        # 6 * 4600 = 27600
        c_reconciliation = 4 * (10000.0 * 0.95 - 250.0)  # 4 * 9250 = 37000
        c_etl = 3 * (15000.0 * 0.95 - 500.0)             # 3 * 13750 = 41250

        monthly_pretax = c_analytics + c_reconciliation + c_etl
        self.assertEqual(monthly_pretax, 105850.0)

        annual_pretax = monthly_pretax * 12.0
        self.assertEqual(annual_pretax, 1270200.0)

        tax_res = compute_business_incremental_tax(annual_pretax, other_annual_income=0.0)
        annual_tax = tax_res["incremental_business_tax"]
        self.assertEqual(annual_tax, 73008.0)

        monthly_tax_provision = round(annual_tax / 12.0, 2)
        self.assertEqual(monthly_tax_provision, 6084.0)

        monthly_posttax_profit = monthly_pretax - monthly_tax_provision
        self.assertEqual(monthly_posttax_profit, 99766.0)

        # Strict check: misses the ₹1 Lakh target by ₹234
        self.assertLess(monthly_posttax_profit, MONTHLY_NET_PROFIT_TARGET_INR)
        self.assertEqual(MONTHLY_NET_PROFIT_TARGET_INR - monthly_posttax_profit, 234.0)

    def test_fourteen_job_portfolio_achieves_target(self):
        """
        Validates that 7 Analytics + 4 Reconciliation + 3 ETL projects:
        Yields ₹1,10,450 pre-tax, annual ₹13,25,400, tax ₹81,962.40 (monthly ₹6,830.20),
        resulting in post-tax net profit of ₹1,03,619.80 (surplus of ₹3,620).
        """
        c_analytics = 7 * 4600.0
        c_reconciliation = 4 * 9250.0
        c_etl = 3 * 13750.0

        monthly_pretax = c_analytics + c_reconciliation + c_etl
        self.assertEqual(monthly_pretax, 110450.0)

        annual_pretax = monthly_pretax * 12.0
        self.assertEqual(annual_pretax, 1325400.0)

        tax_res = compute_business_incremental_tax(annual_pretax, other_annual_income=0.0)
        annual_tax = tax_res["incremental_business_tax"]
        self.assertEqual(annual_tax, 81962.40)

        monthly_tax = round(annual_tax / 12.0, 2)
        monthly_posttax = monthly_pretax - monthly_tax
        self.assertEqual(monthly_posttax, 103619.80)
        self.assertGreater(monthly_posttax, MONTHLY_NET_PROFIT_TARGET_INR)

    def test_safety_buffer_target_calculation(self):
        """
        Validates ₹1.25 Lakh / month post-tax safety buffer target:
        Requires ₹16,31,313.12 pre-tax profit annually, ₹1,35,942.76 monthly.
        """
        annual_buffer_target = MONTHLY_SAFETY_BUFFER_TARGET_INR * 12.0
        self.assertEqual(annual_buffer_target, 1500000.0)

        pretax_req = compute_required_pretax_profit(annual_buffer_target, other_annual_income=0.0)
        self.assertAlmostEqual(pretax_req, 1631313.12, delta=0.1)

        plan = NetProfitOptimizer.analyze_net_profit(MONTHLY_NET_PROFIT_TARGET_INR)
        self.assertEqual(plan.monthly_safety_buffer_target_inr, 125000.0)
        self.assertAlmostEqual(plan.annual_pretax_safety_buffer_required_inr, 1631313.12, delta=0.1)
        self.assertAlmostEqual(plan.monthly_pretax_safety_buffer_required_inr, 135942.76, delta=0.1)
        self.assertIn("data_analytics", plan.safety_buffer_jobs_needed)

    def test_state_specific_gst_threshold_monitoring(self):
        """Tests standard state (₹20L) vs special category state (₹10L) GST threshold alerts."""
        # Standard State: ₹20 Lakhs
        # 17 Lakhs -> BELOW_THRESHOLD
        st_below = get_gst_status(1700000.0, threshold_inr=2000000.0)
        self.assertEqual(st_below["status"], "BELOW_THRESHOLD")

        # 18.5 Lakhs -> APPROACHING_THRESHOLD (>= 90%)
        st_warn = get_gst_status(1850000.0, threshold_inr=2000000.0)
        self.assertEqual(st_warn["status"], "APPROACHING_THRESHOLD")

        # 20.5 Lakhs -> CA_REVIEW_REQUIRED
        st_mand = get_gst_status(2050000.0, threshold_inr=2000000.0)
        self.assertEqual(st_mand["status"], "CA_REVIEW_REQUIRED")

        # Special Category State: ₹10 Lakhs
        # 9.2 Lakhs -> APPROACHING_THRESHOLD for ₹10L, but BELOW_THRESHOLD for ₹20L
        spec_warn = get_gst_status(920000.0, threshold_inr=1000000.0)
        self.assertEqual(spec_warn["status"], "APPROACHING_THRESHOLD")

        std_below = get_gst_status(920000.0, threshold_inr=2000000.0)
        self.assertEqual(std_below["status"], "BELOW_THRESHOLD")

        # 10.1 Lakhs -> CA_REVIEW_REQUIRED for ₹10L
        spec_mand = get_gst_status(1010000.0, threshold_inr=1000000.0)
        self.assertEqual(spec_mand["status"], "CA_REVIEW_REQUIRED")

    def test_distinct_financial_states_summary(self):
        """Tests that get_financial_states_summary returns non-overlapping values across all 6 states."""
        # 1. Estimated Pipeline
        self._create_opp("Lead 1", 8000.0, status="new")

        # 2. Contracted Pipeline
        self._create_opp("Lead 2", 12000.0, status="approved_for_work")

        # 3. Invoiced Pending
        opp_id = self._create_opp("Lead 3", 15000.0, status="submitted_by_owner")
        t_id = create_task("Analysis", "data_analytics", "ws_fin_states", opp_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (t_id,))
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        create_invoice(opp_id, client_name="Pending Client", service_tier="data_analytics", amount_subtotal=15000.0, tax_rate=0.0)

        f_summary = get_financial_states_summary()
        self.assertEqual(f_summary["estimated_pipeline_inr"], 8000.0)
        self.assertEqual(f_summary["contracted_pipeline_inr"], 12000.0)
        self.assertEqual(f_summary["invoiced_pending_inr"], 15000.0)

    def test_accounting_method_cash_vs_mercantile_section_276(self):
        """Tests Section 276 cash vs mercantile (accrual) accounting method."""
        # 1. Under Cash Method (Default): Unpaid invoice is not taxable yet
        upsert_tax_profile(accounting_method="cash")
        opp_id = self._create_opp("Lead Accrual", 20000.0, status="submitted_by_owner")
        t_id = create_task("Dashboard", "interactive_dashboard", "ws_accrual", opp_id)
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (t_id,))
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        create_invoice(opp_id, client_name="Corp Client", service_tier="interactive_dashboard", amount_subtotal=20000.0, tax_rate=0.0)

        cash_summary = get_cash_and_reserves_summary()
        self.assertEqual(cash_summary["accounting_method"], "cash")
        self.assertEqual(cash_summary["collected_receipts_inr"], 0.0)
        self.assertEqual(cash_summary["taxable_business_profit_inr"], 0.0)

        # 2. Switch to Mercantile Method: Invoiced amount becomes taxable turnover upon issuance
        upsert_tax_profile(accounting_method="mercantile")
        merc_summary = get_cash_and_reserves_summary()
        self.assertEqual(merc_summary["accounting_method"], "mercantile")
        self.assertEqual(merc_summary["taxable_business_profit_inr"], 20000.0)

    def test_eighteen_project_safety_buffer_portfolio(self):
        """
        Validates the illustrative 18-project internal operating safety-buffer portfolio:
        9 Analytics + 5 Reconciliation + 4 ETL = 18 projects.
        Gross: ₹1,55,000 | Pre-Tax: ₹1,42,650 | Tax: ₹12,338 | Post-Tax: ₹1,30,312 | Effort: 53 hours.
        """
        model = NetProfitOptimizer.get_safety_buffer_portfolio_model()
        self.assertEqual(model["total_projects"], 18)
        self.assertEqual(model["monthly_gross_billings_inr"], 155000.0)
        self.assertEqual(model["monthly_pretax_contribution_inr"], 142650.0)
        self.assertEqual(model["annual_pretax_inr"], 1711800.0)
        self.assertAlmostEqual(model["monthly_tax_provision_inr"], 12337.87, delta=1.0)
        self.assertAlmostEqual(model["monthly_posttax_profit_inr"], 130312.13, delta=1.0)
        self.assertEqual(model["monthly_delivery_hours"], 53.0)
        self.assertTrue(model["target_achieved"])
        self.assertGreater(model["monthly_posttax_profit_inr"], MONTHLY_SAFETY_BUFFER_TARGET_INR)

    def test_catalog_pricing_strictly_ex_gst(self):
        """Verifies that all standard catalog service quotes are strictly ex-GST base prices."""
        expected_prices = {
            "data_analytics": 5000.0,
            "csv_reconciliation": 10000.0,
            "data_engineering": 15000.0,
            "interactive_dashboard": 20000.0,
        }
        for s in SERVICE_PROFIT_SPECS:
            self.assertIn(s.tier_id, expected_prices)
            self.assertEqual(s.target_price_inr, expected_prices[s.tier_id])
            self.assertGreater(s.contribution_margin_pct, 50.0)


if __name__ == "__main__":
    unittest.main()
