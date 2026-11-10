from __future__ import annotations

"""
SOLOEARN Net Profit Intelligence Engine (M6.1.2)
Replaces naive gross-revenue planning with strict, tax-aware post-tax owner profit optimization:
1. North Star: Minimum ₹1,00,000 / Month AFTER ALL business expenses and income tax (₹12 Lakh / Year).
2. Internal Buffer Target: ₹1,25,000 / Month.
3. Separation of Pipeline, Invoices, Collected Receipts, Operating Profit, Tax Provisions, and Distributable Cash.
4. Dynamic Required Customer Receipts modeling across operating expense ratios (10% to 50%).
5. Service Unit Economics, contribution margin per hour, and minimum profitable quote enforcement.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.db import connect, get_active_tax_profile
from core.financial_ledger import get_cash_and_reserves_summary, get_expenses_summary
from core.tax_engine import (
    compute_business_incremental_tax,
    compute_income_tax_india_2026,
    compute_required_pretax_profit,
    get_gst_status,
    validate_tax_profile,
)

MONTHLY_NET_PROFIT_TARGET_INR = 100000.0  # ₹1 Lakh / Month post-tax
MONTHLY_BUFFER_TARGET_INR = 125000.0      # ₹1.25 Lakh / Month internal buffer
MONTHLY_SAFETY_BUFFER_TARGET_INR = 125000.0  # ₹1.25 Lakh / Month safety buffer
ANNUAL_NET_PROFIT_TARGET_INR = 1200000.0   # ₹12 Lakh / Year post-tax

MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN = 0.50  # 50% minimum margin to accept work


@dataclass
class ServiceProfitSpec:
    tier_id: str
    display_name: str
    target_price_inr: float
    estimated_hours: float
    direct_cost_inr: float
    platform_fee_pct: float
    description: str

    @property
    def net_contribution_inr(self) -> float:
        fee = self.target_price_inr * (self.platform_fee_pct / 100.0)
        return round(self.target_price_inr - self.direct_cost_inr - fee, 2)

    @property
    def contribution_margin_pct(self) -> float:
        if self.target_price_inr <= 0:
            return 0.0
        return round((self.net_contribution_inr / self.target_price_inr) * 100.0, 1)

    @property
    def contribution_per_hour_inr(self) -> float:
        hrs = max(0.25, self.estimated_hours)
        return round(self.net_contribution_inr / hrs, 2)


SERVICE_PROFIT_SPECS: list[ServiceProfitSpec] = [
    ServiceProfitSpec(
        tier_id="data_analytics",
        display_name="Tier 1: Business Analytics & Excel Pack",
        target_price_inr=5000.0,
        estimated_hours=2.0,
        direct_cost_inr=150.0,     # storage/processing
        platform_fee_pct=5.0,      # 5% payment gateway / platform
        description="Multi-tab Excel model with formulas, charts, profile, and executive HTML presentation.",
    ),
    ServiceProfitSpec(
        tier_id="csv_reconciliation",
        display_name="Tier 2: CSV Reconciliation Automation",
        target_price_inr=10000.0,
        estimated_hours=3.0,
        direct_cost_inr=250.0,
        platform_fee_pct=5.0,
        description="Header normalization, exact deduplication, automated Python script, and test suite.",
    ),
    ServiceProfitSpec(
        tier_id="data_engineering",
        display_name="Tier 3: Data Engineering ETL Pipeline",
        target_price_inr=15000.0,
        estimated_hours=5.0,
        direct_cost_inr=500.0,
        platform_fee_pct=5.0,
        description="Production pipeline script, schema manifest, data dictionary, and automated tests.",
    ),
    ServiceProfitSpec(
        tier_id="interactive_dashboard",
        display_name="Tier 4: Executive Interactive HTML5 Dashboard",
        target_price_inr=20000.0,
        estimated_hours=6.0,
        direct_cost_inr=600.0,
        platform_fee_pct=5.0,
        description="Standalone offline HTML5 dashboard with dynamic client-side search, KPI cards, and SVG visuals.",
    ),
]


@dataclass
class NetProfitPlan:
    monthly_net_profit_target_inr: float
    annual_net_profit_target_inr: float
    annual_pretax_profit_required_inr: float
    monthly_pretax_profit_required_inr: float
    observed_expense_ratio_pct: float
    monthly_receipts_required_ex_gst_inr: float
    mtd_collected_receipts_inr: float
    mtd_operating_expenses_inr: float
    mtd_operating_profit_inr: float
    mtd_estimated_income_tax_inr: float
    mtd_net_profit_after_tax_inr: float
    net_profit_gap_inr: float
    progress_pct: float
    distributable_cash_inr: float | None
    gst_status: dict[str, Any]
    service_capacities_needed: dict[str, int]
    recommended_action: str
    bank_balance_known: bool = False
    service_economics: list[dict[str, Any]] = field(default_factory=list)
    monthly_safety_buffer_target_inr: float = MONTHLY_SAFETY_BUFFER_TARGET_INR
    annual_pretax_safety_buffer_required_inr: float = 0.0
    monthly_pretax_safety_buffer_required_inr: float = 0.0
    safety_buffer_gap_inr: float = 0.0
    safety_buffer_jobs_needed: dict[str, int] = field(default_factory=dict)
    accounting_method: str = "cash"


class NetProfitOptimizer:
    """
    Orchestrates financial decisions against the ₹1,00,000 / month post-tax net profit target.
    Derives real pre-tax income requirements, tracks expenses, separates cash from receivables,
    and enforces minimum profitable project pricing.
    """

    @classmethod
    def get_service_economics(cls) -> list[dict[str, Any]]:
        """Returns unit economics and contribution per hour for all 4 verified services."""
        return [
            {
                "tier_id": s.tier_id,
                "display_name": s.display_name,
                "target_price_inr": s.target_price_inr,
                "estimated_hours": s.estimated_hours,
                "direct_cost_inr": s.direct_cost_inr,
                "net_contribution_inr": s.net_contribution_inr,
                "contribution_margin_pct": s.contribution_margin_pct,
                "contribution_per_hour_inr": s.contribution_per_hour_inr,
                "description": s.description,
            }
            for s in SERVICE_PROFIT_SPECS
        ]

    @classmethod
    def get_safety_buffer_portfolio_model(cls) -> dict[str, Any]:
        """
        Returns the illustrative 18-project internal operating safety-buffer portfolio:
        - 9 Analytics @ ₹5,000 ex-GST (contribution: ₹4,600 each, effort: 2h)
        - 5 Reconciliation @ ₹10,000 ex-GST (contribution: ₹9,250 each, effort: 3h)
        - 4 ETL pipelines @ ₹15,000 ex-GST (contribution: ₹13,750 each, effort: 5h)
        Produces ₹1,30,312 monthly post-tax profit after ₹12,338 tax provision and 53 delivery hours.
        """
        mix = [
            ("data_analytics", 9, 5000.0, 4600.0, 2.0),
            ("csv_reconciliation", 5, 10000.0, 9250.0, 3.0),
            ("data_engineering", 4, 15000.0, 13750.0, 5.0),
        ]
        gross_monthly = sum(count * price for _, count, price, _, _ in mix)
        pretax_monthly = sum(count * contrib for _, count, _, contrib, _ in mix)
        delivery_hours = sum(count * hrs for _, count, _, _, hrs in mix)
        annual_pretax = pretax_monthly * 12.0
        tax_res = compute_income_tax_india_2026(annual_pretax)
        monthly_tax = round(tax_res["total_tax"] / 12.0, 2)
        posttax_profit = round(pretax_monthly - monthly_tax, 2)
        return {
            "total_projects": 18,
            "project_mix": {"data_analytics": 9, "csv_reconciliation": 5, "data_engineering": 4},
            "monthly_gross_billings_inr": gross_monthly,
            "monthly_pretax_contribution_inr": pretax_monthly,
            "annual_pretax_inr": annual_pretax,
            "annual_tax_inr": tax_res["total_tax"],
            "monthly_tax_provision_inr": monthly_tax,
            "monthly_posttax_profit_inr": posttax_profit,
            "monthly_delivery_hours": delivery_hours,
            "target_achieved": posttax_profit >= MONTHLY_SAFETY_BUFFER_TARGET_INR,
        }

    @classmethod
    def calculate_minimum_profitable_quote(
        cls,
        estimated_hours: float,
        direct_costs_inr: float = 0.0,
        desired_hourly_return_inr: float = 2000.0,
        platform_fee_pct: float = 5.0,
    ) -> dict[str, Any]:
        """
        Calculates the minimum quote required to maintain profitability above the 50% contribution margin.
        Rejects or flags any project that would yield below minimum threshold.
        """
        hrs = max(0.5, float(estimated_hours))
        direct = max(0.0, float(direct_costs_inr))
        hourly = max(1000.0, float(desired_hourly_return_inr))
        fee_rate = min(0.30, max(0.0, float(platform_fee_pct) / 100.0))

        # Target net earnings = hrs * hourly
        # Quote * (1 - fee_rate) - direct = Target
        # Quote = (Target + direct) / (1 - fee_rate)
        min_quote = round((hrs * hourly + direct) / (1.0 - fee_rate), 2)
        net_contrib = round(min_quote * (1.0 - fee_rate) - direct, 2)
        margin = round((net_contrib / min_quote) * 100.0, 1) if min_quote > 0 else 0.0

        return {
            "estimated_hours": hrs,
            "direct_costs_inr": direct,
            "minimum_quote_inr": min_quote,
            "net_contribution_inr": net_contrib,
            "contribution_margin_pct": margin,
            "effective_hourly_return_inr": round(net_contrib / hrs, 2),
            "is_acceptable": margin >= (MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN * 100.0),
        }

    @classmethod
    def solve_required_receipts(
        cls,
        monthly_target: float,
        cost_ratio: float,
        deductible_ratio: float = 0.0,
        other_annual_income: float = 0.0,
        regime: str = "new_2025",
    ) -> float:
        """
        Solves for the monthly collected receipts required to yield `monthly_target`
        after operating expenses and incremental business income tax.
        """
        if monthly_target <= 0:
            return 0.0
        low = 0.0
        high = max(50000000.0, (monthly_target * 12.0 + other_annual_income) * 4.0 / 12.0)
        for _ in range(60):
            mid = (low + high) / 2.0
            op_profit = (1.0 - cost_ratio) * mid
            taxable_b_annual = max(0.0, (1.0 - deductible_ratio) * mid * 12.0)
            inc_res = compute_business_incremental_tax(taxable_b_annual, other_annual_income, regime=regime)
            tax_monthly = inc_res["incremental_business_tax"] / 12.0
            net_posttax = op_profit - tax_monthly
            if abs(net_posttax - monthly_target) < 0.01:
                return round(mid, 2)
            if net_posttax < monthly_target:
                low = mid
            else:
                high = mid
        return round((low + high) / 2.0, 2)

    @classmethod
    def _jobs_required_for_after_tax_gap(
        cls,
        after_tax_gap_inr: float,
        contribution_per_job_inr: float,
        base_taxable_monthly_inr: float,
        other_annual_income_inr: float,
        regime: str = "new_2025",
    ) -> int:
        """Estimates the minimum whole jobs needed after incremental income tax.

        Service contribution values are currently assumptions. This calculation
        treats incremental contribution as taxable business profit and applies
        the configured resident/new-regime tax model to the incremental amount.
        """
        if after_tax_gap_inr <= 0:
            return 0
        if not math.isfinite(contribution_per_job_inr) or contribution_per_job_inr <= 0:
            return 0

        base_annual = max(0.0, base_taxable_monthly_inr) * 12.0
        base_tax = compute_business_incremental_tax(
            base_annual, other_annual_income_inr, regime=regime
        )["incremental_business_tax"]

        def net_after_tax(job_count: int) -> float:
            extra_pre_tax = job_count * contribution_per_job_inr
            total_tax = compute_business_incremental_tax(
                base_annual + extra_pre_tax * 12.0,
                other_annual_income_inr,
                regime=regime,
            )["incremental_business_tax"]
            incremental_tax_monthly = max(0.0, total_tax - base_tax) / 12.0
            return extra_pre_tax - incremental_tax_monthly

        low, high = 0, max(1, math.ceil(after_tax_gap_inr / contribution_per_job_inr))
        while net_after_tax(high) < after_tax_gap_inr:
            high *= 2
            if high > 1_000_000:
                raise ValueError("Required job count exceeds supported planning limit")

        while low < high:
            mid = (low + high) // 2
            if net_after_tax(mid) >= after_tax_gap_inr:
                high = mid
            else:
                low = mid + 1
        return low

    @classmethod
    def analyze_net_profit(
        cls,
        monthly_target_inr: float = MONTHLY_NET_PROFIT_TARGET_INR,
    ) -> NetProfitPlan:
        """
        Performs full net profit analysis for the current month:
        1. Computes pre-tax profit required for target post-tax profit accounting for other income.
        2. Gathers actual collected receipts and actual business expenses.
        3. Computes operating profit, models income tax, and determines post-tax profit.
        4. Calculates required receipts using observed expense ratio and tax deductibility ratio.
        """
        if not math.isfinite(monthly_target_inr) or monthly_target_inr <= 0:
            raise ValueError("Monthly net profit target must be a positive finite number")

        # Active tax profile (other income, regime, etc.)
        tax_prof = get_active_tax_profile()
        tax_settings = validate_tax_profile(tax_prof)
        other_annual_income = float(tax_prof["other_annual_income_inr"]) if tax_prof else 0.0

        annual_target = monthly_target_inr * 12.0
        annual_pretax_required = compute_required_pretax_profit(
            annual_target, other_annual_income, regime=tax_settings["regime"]
        )
        monthly_pretax_required = round(annual_pretax_required / 12.0, 2)

        # Retrieve cash, receipts, expenses, and reserves
        cash_res = get_cash_and_reserves_summary()
        collected_receipts = cash_res["collected_receipts_inr"]
        operating_expenses = cash_res["total_operating_expenses_inr"]
        deductible_expenses = cash_res.get("deductible_operating_expenses_inr", 0.0)
        operating_profit = cash_res["operating_profit_inr"]
        taxable_business_profit = cash_res.get("taxable_business_profit_inr", operating_profit)

        # Annualized tax estimate for current run-rate
        inc_tax_res = compute_business_incremental_tax(taxable_business_profit * 12.0, other_annual_income)
        mtd_income_tax = round(inc_tax_res["incremental_business_tax"] / 12.0, 2)

        # Net Profit After Tax
        mtd_net_profit = round(operating_profit - mtd_income_tax, 2)
        gap = max(0.0, round(monthly_target_inr - mtd_net_profit, 2))
        progress = min(100.0, max(0.0, round((mtd_net_profit / monthly_target_inr) * 100.0, 1)))

        # Observed operating expense ratio & deductible ratio:
        if collected_receipts > 0:
            cost_ratio = min(0.80, max(0.0, operating_expenses / collected_receipts))
            deductible_ratio = min(cost_ratio, max(0.0, deductible_expenses / collected_receipts))
        else:
            cost_ratio = 0.25      # Default 25% planning assumption
            deductible_ratio = 0.0  # 0% tax deductions until reviewed

        # Monthly Receipts Required solved via binary search
        monthly_receipts_required = cls.solve_required_receipts(
            monthly_target=monthly_target_inr,
            cost_ratio=cost_ratio,
            deductible_ratio=deductible_ratio,
            other_annual_income=other_annual_income,
            regime=tax_settings["regime"],
        )

        # GST aggregate turnover is based on supplies, not merely invoices already paid.
        # Include all non-cancelled SOLOEARN invoices issued within the configured tax year
        # plus owner-entered turnover from other activities under the same PAN.
        tax_year = str(tax_prof.get("tax_year", "2026-27")) if tax_prof else "2026-27"
        fy_start_year = int(tax_year[:4])
        fy_start = f"{fy_start_year:04d}-04-01"
        fy_end = f"{fy_start_year + 1:04d}-04-01"
        with connect() as conn:
            row_inv = conn.execute(
                """SELECT COALESCE(SUM(amount_subtotal), 0)
                   FROM invoices
                   WHERE status <> 'cancelled'
                     AND substr(issued_at, 1, 10) >= ?
                     AND substr(issued_at, 1, 10) < ?""",
                (fy_start, fy_end),
            ).fetchone()
            invoice_turnover = float(row_inv[0] if row_inv else 0.0)
        other_pan_turnover = float(tax_prof.get("other_pan_aggregate_turnover_inr", 0.0)) if tax_prof else 0.0
        gst_threshold = float(tax_prof.get("gst_threshold_inr", 2000000.0)) if tax_prof else 2000000.0
        total_turnover = invoice_turnover + other_pan_turnover
        gst_info = get_gst_status(total_turnover, threshold_inr=gst_threshold)
        gst_info["soloearn_invoice_turnover_inr"] = round(invoice_turnover, 2)
        gst_info["other_same_pan_turnover_inr"] = round(other_pan_turnover, 2)
        gst_info["turnover_basis"] = "FY non-cancelled invoice subtotals plus owner-entered other aggregate turnover under the same PAN; CA review required"

        # Estimate whole-job counts after incremental income tax, not by comparing
        # pre-tax service contribution directly with the post-tax profit gap.
        average_contribution = sum(s.net_contribution_inr for s in SERVICE_PROFIT_SPECS) / len(SERVICE_PROFIT_SPECS)
        jobs_needed_total = cls._jobs_required_for_after_tax_gap(
            gap, average_contribution, taxable_business_profit,
            other_annual_income, regime=tax_settings["regime"],
        ) if gap > 0 else 0

        needed_by_tier = {
            s.tier_id: cls._jobs_required_for_after_tax_gap(
                gap, s.net_contribution_inr, taxable_business_profit,
                other_annual_income, regime=tax_settings["regime"],
            ) if gap > 0 else 0
            for s in SERVICE_PROFIT_SPECS
        }

        # Safety buffer modeling (target: ₹1.25 Lakh / month post-tax)
        annual_buffer_target = MONTHLY_SAFETY_BUFFER_TARGET_INR * 12.0
        annual_pretax_buffer_req = compute_required_pretax_profit(
            annual_buffer_target, other_annual_income, regime=tax_settings["regime"]
        )
        monthly_pretax_buffer_req = round(annual_pretax_buffer_req / 12.0, 2)
        buffer_gap = max(0.0, round(MONTHLY_SAFETY_BUFFER_TARGET_INR - mtd_net_profit, 2))
        buffer_needed_by_tier = {
            s.tier_id: cls._jobs_required_for_after_tax_gap(
                buffer_gap, s.net_contribution_inr, taxable_business_profit,
                other_annual_income, regime=tax_settings["regime"],
            ) if buffer_gap > 0 else 0
            for s in SERVICE_PROFIT_SPECS
        }

        # Recommendations
        distrib_str = (
            f"₹{cash_res['distributable_cash_inr']:,.2f}"
            if cash_res.get("distributable_cash_inr") is not None
            else "unknown (opening balance unconfirmed)"
        )

        if gap <= 0:
            rec = (
                f"Net profit target achieved: ₹{mtd_net_profit:,.2f} confirmed post-tax profit this month "
                f"(after ₹{operating_expenses:,.2f} in expenses and ₹{mtd_income_tax:,.2f} in estimated tax). "
                f"Distributable cash available: {distrib_str}."
            )
        else:
            rec = (
                f"Current post-tax net profit gap is ₹{gap:,.2f} this month. "
                f"At your observed {cost_ratio * 100:.1f}% cost ratio, you require approximately "
                f"₹{monthly_receipts_required:,.2f} in gross collected receipts (ex-GST) to produce "
                f"₹{monthly_target_inr:,.2f} in take-home profit. "
                f"This requires delivering approximately {needed_by_tier.get('data_analytics', 0)} Excel packs or "
                f"{needed_by_tier.get('data_engineering', 0)} ETL pipelines. "
                f"Distributable cash: {distrib_str}. "
                "Job counts are planning estimates based on assumed service contribution and estimated incremental tax; "
                "they do not yet allocate unmeasured shared overhead, customer-acquisition cost, or rework."
            )

        return NetProfitPlan(
            monthly_net_profit_target_inr=monthly_target_inr,
            annual_net_profit_target_inr=annual_target,
            annual_pretax_profit_required_inr=annual_pretax_required,
            monthly_pretax_profit_required_inr=monthly_pretax_required,
            observed_expense_ratio_pct=round(cost_ratio * 100.0, 1),
            monthly_receipts_required_ex_gst_inr=monthly_receipts_required,
            mtd_collected_receipts_inr=collected_receipts,
            mtd_operating_expenses_inr=operating_expenses,
            mtd_operating_profit_inr=operating_profit,
            mtd_estimated_income_tax_inr=mtd_income_tax,
            mtd_net_profit_after_tax_inr=mtd_net_profit,
            net_profit_gap_inr=gap,
            progress_pct=progress,
            distributable_cash_inr=cash_res["distributable_cash_inr"],
            bank_balance_known=cash_res.get("bank_balance_known", False),
            gst_status=gst_info,
            service_capacities_needed={
                "total_jobs_needed": jobs_needed_total,
                **needed_by_tier,
            },
            recommended_action=rec,
            service_economics=cls.get_service_economics(),
            monthly_safety_buffer_target_inr=MONTHLY_SAFETY_BUFFER_TARGET_INR,
            annual_pretax_safety_buffer_required_inr=annual_pretax_buffer_req,
            monthly_pretax_safety_buffer_required_inr=monthly_pretax_buffer_req,
            safety_buffer_gap_inr=buffer_gap,
            safety_buffer_jobs_needed=buffer_needed_by_tier,
            accounting_method=cash_res.get("accounting_method", "cash"),
        )
