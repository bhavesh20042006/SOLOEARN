from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.db import connect


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
        display_name="Tier 2: Business Process Automation Script",
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
    Engineered to strictly guide and optimize SOLOEARN operations towards
    achieving a minimum of ₹1,00,000 / month in verified revenue.
    """

    @classmethod
    def get_optimal_mix(cls) -> list[dict[str, Any]]:
        """Returns the proven, realistic monthly service mix to reach ₹1,05,000."""
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

            # Invoiced revenue awaiting receipt confirmation
            row_inv = conn.execute(
                """SELECT COALESCE(SUM(amount_total), 0) FROM invoices 
                   WHERE status IN ('issued', 'payment_pending')"""
            ).fetchone()
            invoiced_pending = float(row_inv[0] if row_inv else 0.0)

            # Pipeline active: approved or in-progress opportunities
            row_pipe = conn.execute(
                """SELECT COALESCE(SUM(amount_inr), 0) FROM opportunities 
                   WHERE status IN ('approved_for_work', 'in_progress', 'submission_ready')"""
            ).fetchone()
            pipeline_active = float(row_pipe[0] if row_pipe else 0.0)

        gap = max(0.0, target_inr - mtd_paid)
        progress = min(100.0, round((mtd_paid / target_inr) * 100.0, 1)) if target_inr > 0 else 100.0

        # Calculate jobs needed to bridge remaining gap
        tier1_price = 5000.0
        tier2_price = 15000.0
        tier3_price = 20000.0

        needed_t1 = math.ceil(gap / tier1_price) if gap > 0 else 0
        needed_t2 = math.ceil(gap / tier2_price) if gap > 0 else 0

        if gap <= 0:
            rec = f"Target Achieved! You have reached ₹{mtd_paid:,.0f} this month (100%+ of ₹{target_inr:,.0f} goal)."
        elif invoiced_pending >= gap:
            rec = f"Pending invoices total ₹{invoiced_pending:,.0f}, which covers the remaining ₹{gap:,.0f} gap. Follow up on payment receipts and verify bank UTRs."
        elif (invoiced_pending + pipeline_active) >= gap:
            rec = f"Active pipeline (₹{pipeline_active:,.0f}) + invoices (₹{invoiced_pending:,.0f}) are sufficient to hit ₹{target_inr:,.0f}. Focus on executing current jobs and running QA."
        else:
            shortfall = gap - (invoiced_pending + pipeline_active)
            rec = f"Gap of ₹{gap:,.0f} remaining. Action: Poll discovery feeds and authorize {needed_t1} Tier-1 proposals or {needed_t2} Tier-2 pipeline proposals."

        return MonthlyRevenuePlan(
            monthly_target_inr=target_inr,
            mtd_paid_inr=mtd_paid,
            invoiced_pending_inr=invoiced_pending,
            pipeline_active_inr=pipeline_active,
            target_gap_inr=gap,
            progress_pct=progress,
            recommended_jobs_needed={"tier1_analytics": needed_t1, "tier2_engineering": needed_t2},
            recommended_action=rec,
            optimal_monthly_mix=cls.get_optimal_mix(),
        )

    @classmethod
    def rank_opportunities_by_hourly_yield(cls, opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Sorts opportunities by risk-adjusted hourly yield:
        hourly_yield = (amount_inr * payment_confidence * skill_match) / estimated_hours
        Prioritizes the most profitable, low-risk jobs for the owner.
        """
        def get_yield(opp: dict[str, Any]) -> float:
            amt = float(opp.get("amount_inr", 0))
            hrs = max(0.5, float(opp.get("estimated_hours", 1)))
            p_conf = float(opp.get("payment_confidence", 0.5))
            s_match = float(opp.get("skill_match", 0.5))
            risk = float(opp.get("risk_level", 5.0))
            risk_penalty = max(0.1, 1.0 - (risk / 15.0))
            return (amt * p_conf * s_match * risk_penalty) / hrs

        return sorted(opportunities, key=get_yield, reverse=True)
