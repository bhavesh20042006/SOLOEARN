from __future__ import annotations

"""
SOLOEARN Net Profit Intelligence Engine (M6.1 & M6.2)
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

from core.db import connect
from core.financial_ledger import get_cash_and_reserves_summary, get_expenses_summary
from core.tax_engine import (
    compute_income_tax_india_2026,
    compute_required_pretax_profit,
    get_gst_status,
)

MONTHLY_NET_PROFIT_TARGET_INR = 100000.0  # ₹1 Lakh / Month post-tax
MONTHLY_BUFFER_TARGET_INR = 125000.0      # ₹1.25 Lakh / Month internal buffer
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
    distributable_cash_inr: float
    gst_status: dict[str, Any]
    service_capacities_needed: dict[str, int]
    recommended_action: str
    service_economics: list[dict[str, Any]] = field(default_factory=list)


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
    def analyze_net_profit(
        cls,
        monthly_target_inr: float = MONTHLY_NET_PROFIT_TARGET_INR,
    ) -> NetProfitPlan:
        """
        Performs full net profit analysis for the current month:
        1. Computes pre-tax profit required for target post-tax profit.
        2. Gathers actual collected receipts and actual business expenses.
        3. Computes operating profit, models income tax, and determines post-tax profit.
        4. Calculates required receipts using observed expense ratio.
        """
        if not math.isfinite(monthly_target_inr) or monthly_target_inr <= 0:
            raise ValueError("Monthly net profit target must be a positive finite number")

        annual_target = monthly_target_inr * 12.0
        annual_pretax_required = compute_required_pretax_profit(annual_target)
        monthly_pretax_required = round(annual_pretax_required / 12.0, 2)

        # Retrieve cash, receipts, expenses, and reserves
        cash_res = get_cash_and_reserves_summary()
        collected_receipts = cash_res["collected_receipts_inr"]
        operating_expenses = cash_res["total_operating_expenses_inr"]
        operating_profit = max(0.0, round(collected_receipts - operating_expenses, 2))

        # Annualized tax estimate for current run-rate
        tax_res = compute_income_tax_india_2026(operating_profit * 12.0)
        mtd_income_tax = round(tax_res["total_tax"] / 12.0, 2)

        # Net Profit After Tax
        mtd_net_profit = max(0.0, round(operating_profit - mtd_income_tax, 2))
        gap = max(0.0, round(monthly_target_inr - mtd_net_profit, 2))
        progress = min(100.0, round((mtd_net_profit / monthly_target_inr) * 100.0, 1))

        # Observed operating expense ratio:
        if collected_receipts > 0:
            observed_ratio = min(0.80, max(0.05, operating_expenses / collected_receipts))
        else:
            observed_ratio = 0.20  # Default 20% planning assumption

        # Monthly Receipts Required = Pretax Required / (1 - expense_ratio)
        monthly_receipts_required = round(monthly_pretax_required / (1.0 - observed_ratio), 2)

        # GST Status
        # Annualized turnover estimate:
        with connect() as conn:
            row_inv = conn.execute(
                "SELECT COALESCE(SUM(amount_subtotal), 0) FROM invoices WHERE status = 'paid'"
            ).fetchone()
            total_turnover = float(row_inv[0] if row_inv else 0.0)

        gst_info = get_gst_status(total_turnover)

        # Capacity needed to close remaining gap:
        # Use average net contribution across service catalog (~₹10,000 per project)
        avg_contrib = 10000.0
        jobs_needed_total = math.ceil(gap / avg_contrib) if gap > 0 else 0

        needed_by_tier = {
            s.tier_id: math.ceil(gap / s.net_contribution_inr) if gap > 0 else 0
            for s in SERVICE_PROFIT_SPECS
        }

        # Recommendations
        if gap <= 0:
            rec = (
                f"Net profit target achieved: ₹{mtd_net_profit:,.2f} confirmed post-tax profit this month "
                f"(after ₹{operating_expenses:,.2f} in expenses and ₹{mtd_income_tax:,.2f} in estimated tax). "
                f"Distributable cash available: ₹{cash_res['distributable_cash_inr']:,.2f}."
            )
        else:
            rec = (
                f"Current post-tax net profit gap is ₹{gap:,.2f} this month. "
                f"At your observed {observed_ratio * 100:.1f}% cost ratio, you require approximately "
                f"₹{monthly_receipts_required:,.2f} in gross collected receipts (ex-GST) to produce "
                f"₹{monthly_target_inr:,.2f} in take-home profit. "
                f"This requires delivering approximately {needed_by_tier.get('data_analytics', 0)} Excel packs or "
                f"{needed_by_tier.get('data_engineering', 0)} ETL pipelines."
            )

        return NetProfitPlan(
            monthly_net_profit_target_inr=monthly_target_inr,
            annual_net_profit_target_inr=annual_target,
            annual_pretax_profit_required_inr=annual_pretax_required,
            monthly_pretax_profit_required_inr=monthly_pretax_required,
            observed_expense_ratio_pct=round(observed_ratio * 100.0, 1),
            monthly_receipts_required_ex_gst_inr=monthly_receipts_required,
            mtd_collected_receipts_inr=collected_receipts,
            mtd_operating_expenses_inr=operating_expenses,
            mtd_operating_profit_inr=operating_profit,
            mtd_estimated_income_tax_inr=mtd_income_tax,
            mtd_net_profit_after_tax_inr=mtd_net_profit,
            net_profit_gap_inr=gap,
            progress_pct=progress,
            distributable_cash_inr=cash_res["distributable_cash_inr"],
            gst_status=gst_info,
            service_capacities_needed={
                "total_jobs_needed": jobs_needed_total,
                **needed_by_tier,
            },
            recommended_action=rec,
            service_economics=cls.get_service_economics(),
        )
