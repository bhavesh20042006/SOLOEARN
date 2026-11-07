from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.db import connect
from core.net_profit_optimizer import (
    NetProfitOptimizer,
    NetProfitPlan,
    MONTHLY_NET_PROFIT_TARGET_INR,
    MONTHLY_BUFFER_TARGET_INR,
)


MONTHLY_REVENUE_TARGET_INR = 100000.0  # ₹1,00,000 / Month


@dataclass
class ServiceTierSpec:
    tier_id: str
    display_name: str
    base_price_inr: float
    avg_hours: float
    effective_rate_hourly: float
    description: str


SERVICE_CATALOG: list[ServiceTierSpec] = [
    ServiceTierSpec(
        tier_id="data_analytics",
        display_name="Tier 1: Business Data Analytics & Excel Pack",
        base_price_inr=5000.0,
        avg_hours=2.5,
        effective_rate_hourly=2000.0,
        description="CSV cleaning, multi-tab Excel workbook with formulas, charts, statistical profile & HTML report.",
    ),
    ServiceTierSpec(
        tier_id="data_engineering",
        display_name="Tier 2: Data Engineering & Automated Pipeline",
        base_price_inr=15000.0,
        avg_hours=6.0,
        effective_rate_hourly=2500.0,
        description="ETL script, automated schema manifest, data dictionary, cleaned output, and pipeline test suite.",
    ),
    ServiceTierSpec(
        tier_id="interactive_dashboard",
        display_name="Tier 3: Executive Interactive HTML5 Dashboard",
        base_price_inr=20000.0,
        avg_hours=8.0,
        effective_rate_hourly=2500.0,
        description="Standalone offline-ready interactive dashboard with client-side search, KPI cards, and SVG visuals.",
    ),
    ServiceTierSpec(
        tier_id="business_automation",
        display_name="Tier 4: CSV Reconciliation Automation Pack",
        base_price_inr=15000.0,
        avg_hours=6.0,
        effective_rate_hourly=2500.0,
        description="Modular Python script with JSON configuration, error handling, logging, and unit tests.",
    ),
]


@dataclass
class MonthlyRevenuePlan:
    monthly_target_inr: float
    mtd_paid_inr: float
    invoiced_pending_inr: float
    pipeline_active_inr: float
    target_gap_inr: float
    progress_pct: float
    recommended_jobs_needed: dict[str, int]
    recommended_action: str
    optimal_monthly_mix: list[dict[str, Any]] = field(default_factory=list)


class RevenueOptimizer:
    """
    Revenue Target and Unit Economics Engine.
    Provides illustrative planning metrics for a gross paid-revenue target of ₹1,00,000 / month. It cannot guarantee customers, sales, or receipts.
    """

    @classmethod
    def get_optimal_mix(cls) -> list[dict[str, Any]]:
        """Returns an illustrative gross-billing mix; prices and demand are not validated."""
        return [
            {
                "tier": "Tier 1: Data Analytics & Excel Pack",
                "price": 5000.0,
                "count": 8,
                "subtotal": 40000.0,
                "total_hours": 20.0,
            },
            {
                "tier": "Tier 2: Data Engineering Pipelines",
                "price": 15000.0,
                "count": 3,
                "subtotal": 45000.0,
                "total_hours": 18.0,
            },
            {
                "tier": "Tier 3: Interactive Executive Dashboard",
                "price": 20000.0,
                "count": 1,
                "subtotal": 20000.0,
                "total_hours": 8.0,
            },
        ]

    @classmethod
    def analyze_monthly_target(cls, target_inr: float = MONTHLY_REVENUE_TARGET_INR) -> MonthlyRevenuePlan:
        try:
            target_inr = float(target_inr)
        except (TypeError, ValueError) as exc:
            raise ValueError("Monthly target must be a valid number") from exc
        if not math.isfinite(target_inr) or target_inr <= 0:
            raise ValueError("Monthly target must be finite and greater than zero")
        now = datetime.now(timezone.utc)
        current_year_month = now.strftime("%Y-%m")

        with connect() as conn:
            # Verified paid revenue in current calendar month
            row_paid = conn.execute(
                """SELECT COALESCE(SUM(amount_inr), 0) FROM revenue 
                   WHERE status = 'paid' AND paid_at LIKE ?""",
                (f"{current_year_month}%",),
            ).fetchone()
            mtd_paid = float(row_paid[0] if row_paid else 0.0)

            # All outstanding invoices (not banked revenue; may relate to other months).
            row_inv = conn.execute(
                """SELECT COALESCE(SUM(amount_total), 0) FROM invoices 
                   WHERE status IN ('issued', 'payment_pending')"""
            ).fetchone()
            invoiced_pending = float(row_inv[0] if row_inv else 0.0)

            # Active uncontracted opportunity value (not guaranteed sales).
            row_pipe = conn.execute(
                """SELECT COALESCE(SUM(amount_inr), 0) FROM opportunities 
                   WHERE status IN ('approved_for_work', 'in_progress', 'submission_ready')"""
            ).fetchone()
            pipeline_active = float(row_pipe[0] if row_pipe else 0.0)

        gap = max(0.0, target_inr - mtd_paid)
        progress = min(100.0, round((mtd_paid / target_inr) * 100.0, 1)) if target_inr > 0 else 100.0

        # Calculate jobs needed to bridge remaining gap
        needed_by_tier = {
            service.tier_id: math.ceil(gap / service.base_price_inr) if gap > 0 else 0
            for service in SERVICE_CATALOG
            if service.base_price_inr > 0
        }
        needed_t1 = needed_by_tier.get("data_analytics", 0)
        needed_t2 = needed_by_tier.get("data_engineering", 0)

        if gap <= 0:
            rec = f"Gross paid-revenue target reached: ₹{mtd_paid:,.0f} owner-confirmed this month against ₹{target_inr:,.0f}. This is gross receipts, not net profit or take-home income."
        elif invoiced_pending >= gap:
            rec = (
                f"₹{invoiced_pending:,.0f} in outstanding invoices exists across all periods. These are not paid revenue "
                f"and may not be collected this month; the current-month paid-revenue gap remains ₹{gap:,.0f}. "
                "Follow up on due invoices and record receipt only after independent bank-statement confirmation."
            )
        elif (invoiced_pending + pipeline_active) >= gap:
            rec = (
                f"Outstanding invoices (₹{invoiced_pending:,.0f}) plus uncontracted active pipeline estimates "
                f"(₹{pipeline_active:,.0f}) nominally exceed the ₹{gap:,.0f} current-month gap. Neither is paid revenue; "
                "the target counts only when funds are actually received and owner-confirmed."
            )
        else:
            shortfall = gap - (invoiced_pending + pipeline_active)
            rec = (
                f"₹{gap:,.0f} in current-month gross paid revenue remains. Outstanding invoices: ₹{invoiced_pending:,.0f}; "
                f"uncontracted pipeline estimate: ₹{pipeline_active:,.0f}; additional estimated value needed: ₹{shortfall:,.0f}. "
                f"Review leads and scope before authorizing any of the indicative {needed_t1} analytics or {needed_t2} data-engineering jobs."
            )

        return MonthlyRevenuePlan(
            monthly_target_inr=target_inr,
            mtd_paid_inr=mtd_paid,
            invoiced_pending_inr=invoiced_pending,
            pipeline_active_inr=pipeline_active,
            target_gap_inr=gap,
            progress_pct=progress,
            recommended_jobs_needed={
                "tier1_analytics": needed_t1,
                "tier2_engineering": needed_t2,
                **needed_by_tier,
            },
            recommended_action=rec,
            optimal_monthly_mix=cls.get_optimal_mix(),
        )

    @classmethod
    def rank_opportunities_by_hourly_yield(cls, opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rank leads by estimated risk-adjusted gross yield per delivery hour.

        This is a screening heuristic, not net profit, a trained payment probability,
        a market quote, or a guarantee of payment. Inputs are clamped to safe ranges.
        Returned rows are copies with the computed estimate attached.
        """
        def get_yield(opp: dict[str, Any]) -> float:
            def finite_value(key: str, default: float) -> float:
                try:
                    value = float(opp.get(key, default))
                    return value if math.isfinite(value) else default
                except (TypeError, ValueError):
                    return default

            amt = max(0.0, finite_value("amount_inr", 0.0))
            hrs = max(0.5, finite_value("estimated_hours", 1.0))
            p_conf = min(1.0, max(0.0, finite_value("payment_confidence", 0.5)))
            s_match = min(1.0, max(0.0, finite_value("skill_match", 0.5)))
            risk = min(10.0, max(0.0, finite_value("risk_level", 5.0)))
            risk_penalty = max(0.1, 1.0 - (risk / 15.0))
            return (amt * p_conf * s_match * risk_penalty) / hrs

        scored: list[dict[str, Any]] = []
        for opportunity in opportunities:
            row = dict(opportunity)
            row["risk_adjusted_gross_yield_inr_per_hour"] = round(get_yield(opportunity), 2)
            scored.append(row)
        return sorted(scored, key=lambda item: item["risk_adjusted_gross_yield_inr_per_hour"], reverse=True)
