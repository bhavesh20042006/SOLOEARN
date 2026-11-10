from __future__ import annotations

"""
SOLOEARN Commercial Readiness & Client Acquisition Engine (M6.2)
Implements:
1. The 50/30/5/3 experimental customer acquisition funnel tracking.
2. Prospective client lead management with verified stages:
   - 'identified' (Target: 50)
   - 'approached' (Target: 30)
   - 'in_conversation' (Target: 5)
   - 'paid_pilot' (Target: 3)
   - 'delivered'
   - 'lost'
3. Exact post-tax project unit economics and hourly yield calculation.
"""

from datetime import datetime, timezone
from typing import Any
from core.db import connect, utc_now, get_active_tax_profile
from core.tax_engine import compute_business_incremental_tax

VALID_FUNNEL_STAGES = {
    "identified",
    "approached",
    "in_conversation",
    "paid_pilot",
    "delivered",
    "lost",
}

VALID_INDUSTRIES = {
    "ecommerce",
    "retail",
    "wholesale",
    "accounting",
    "services",
    "other",
}

VALID_CHANNELS = {
    "linkedin",
    "email",
    "phone_whatsapp",
    "direct_network",
    "freelance_platform",
    "other",
}


def init_commercial_tables() -> None:
    """Creates commercial leads table if not already present."""
    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS commercial_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                contact_channel TEXT NOT NULL DEFAULT 'linkedin',
                contact_handle TEXT DEFAULT '',
                industry TEXT NOT NULL DEFAULT 'ecommerce',
                service_tier TEXT NOT NULL DEFAULT 'data_analytics',
                proposed_price_inr REAL NOT NULL DEFAULT 5000.0,
                stage TEXT NOT NULL DEFAULT 'identified',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );"""
        )
        conn.commit()


def add_commercial_lead(
    company_name: str,
    contact_name: str,
    contact_channel: str = "linkedin",
    contact_handle: str = "",
    industry: str = "ecommerce",
    service_tier: str = "data_analytics",
    proposed_price_inr: float = 5000.0,
    stage: str = "identified",
    notes: str = "",
) -> int:
    """Registers a qualified prospect in the acquisition funnel."""
    init_commercial_tables()
    company = company_name.strip()
    contact = contact_name.strip()
    if not company:
        raise ValueError("Company name is required")
    if not contact:
        raise ValueError("Contact name is required")
    if stage not in VALID_FUNNEL_STAGES:
        raise ValueError(f"Invalid funnel stage: '{stage}'. Allowed: {VALID_FUNNEL_STAGES}")
    if contact_channel not in VALID_CHANNELS:
        raise ValueError(f"Invalid contact channel: '{contact_channel}'. Allowed: {VALID_CHANNELS}")
    if industry not in VALID_INDUSTRIES:
        industry = "other"
    if proposed_price_inr < 1.0:
        raise ValueError("Proposed price must be at least ₹1.0")

    now = utc_now()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO commercial_leads (
                company_name, contact_name, contact_channel, contact_handle,
                industry, service_tier, proposed_price_inr, stage, notes,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company[:180],
                contact[:120],
                contact_channel,
                contact_handle[:200],
                industry,
                service_tier,
                float(proposed_price_inr),
                stage,
                notes[:2000],
                now,
                now,
            ),
        )
        lead_id = int(cur.lastrowid)
        conn.commit()
    return lead_id


def update_lead_stage(lead_id: int, new_stage: str, notes: str | None = None) -> bool:
    """Transitions a lead along the acquisition funnel."""
    init_commercial_tables()
    if new_stage not in VALID_FUNNEL_STAGES:
        raise ValueError(f"Invalid funnel stage: '{new_stage}'. Allowed: {VALID_FUNNEL_STAGES}")

    now = utc_now()
    with connect() as conn:
        if notes is not None:
            cur = conn.execute(
                """UPDATE commercial_leads
                   SET stage = ?, notes = ?, updated_at = ?
                   WHERE id = ?""",
                (new_stage, notes[:2000], now, lead_id),
            )
        else:
            cur = conn.execute(
                """UPDATE commercial_leads
                   SET stage = ?, updated_at = ?
                   WHERE id = ?""",
                (new_stage, now, lead_id),
            )
        conn.commit()
        return cur.rowcount > 0


def list_commercial_leads(stage: str | None = None) -> list[dict[str, Any]]:
    """Returns commercial leads, optionally filtered by funnel stage."""
    init_commercial_tables()
    with connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        if stage:
            rows = conn.execute(
                "SELECT * FROM commercial_leads WHERE stage = ? ORDER BY id DESC",
                (stage,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM commercial_leads ORDER BY id DESC"
            ).fetchall()
        return list(rows)


def get_funnel_metrics() -> dict[str, Any]:
    """Computes progress against the 50/30/5/3 experimental targets."""
    init_commercial_tables()
    with connect() as conn:
        counts = {s: 0 for s in VALID_FUNNEL_STAGES}
        cur = conn.execute(
            "SELECT stage, COUNT(*) FROM commercial_leads GROUP BY stage"
        )
        for stage, count in cur.fetchall():
            if stage in counts:
                counts[stage] = count

    # In funnel logic:
    # 'approached' or beyond includes approached, in_conversation, paid_pilot, delivered
    total_identified = sum(counts.values())
    reached_approached = counts["approached"] + counts["in_conversation"] + counts["paid_pilot"] + counts["delivered"]
    reached_conversation = counts["in_conversation"] + counts["paid_pilot"] + counts["delivered"]
    reached_pilot = counts["paid_pilot"] + counts["delivered"]

    return {
        "identified_count": total_identified,
        "identified_target": 50,
        "identified_progress_pct": min(100.0, round((total_identified / 50.0) * 100, 1)),
        "approached_count": reached_approached,
        "approached_target": 30,
        "approached_progress_pct": min(100.0, round((reached_approached / 30.0) * 100, 1)),
        "in_conversation_count": reached_conversation,
        "in_conversation_target": 5,
        "in_conversation_progress_pct": min(100.0, round((reached_conversation / 5.0) * 100, 1)),
        "paid_pilot_count": reached_pilot,
        "paid_pilot_target": 3,
        "paid_pilot_progress_pct": min(100.0, round((reached_pilot / 3.0) * 100, 1)),
        "stage_breakdown": counts,
    }


def calculate_project_unit_economics(
    gross_price_inr: float,
    platform_fee_pct: float = 5.0,
    direct_cost_inr: float = 150.0,
    estimated_hours: float = 2.0,
    other_income_inr: float | None = None,
) -> dict[str, Any]:
    """
    Computes exact after-tax owner profit and hourly yield for a project quote.
    Directly incorporates Indian income tax under the Income-tax Act, 2025.
    """
    if gross_price_inr <= 0:
        raise ValueError("Gross price must be positive")
    if estimated_hours <= 0:
        raise ValueError("Estimated hours must be positive")
    if platform_fee_pct < 0 or platform_fee_pct > 50:
        raise ValueError("Platform fee pct must be between 0% and 50%")

    fee_amount = round(gross_price_inr * (platform_fee_pct / 100.0), 2)
    net_pretax_receipt = round(gross_price_inr - fee_amount - direct_cost_inr, 2)
    contribution_margin_pct = round((net_pretax_receipt / gross_price_inr) * 100, 2)

    # Tax reserve calculation
    if other_income_inr is None:
        profile = get_active_tax_profile()
        other_inc = profile.get("other_taxable_income_inr", 0.0) if profile else 0.0
    else:
        other_inc = other_income_inr

    tax_calc = compute_business_incremental_tax(
        business_taxable_income=max(0.0, net_pretax_receipt),
        other_annual_income=other_inc,
    )
    incremental_tax = round(tax_calc["incremental_business_tax"], 2)
    net_owner_profit = round(net_pretax_receipt - incremental_tax, 2)
    hourly_yield_inr = round(net_owner_profit / estimated_hours, 2)

    return {
        "gross_price_inr": gross_price_inr,
        "platform_fee_inr": fee_amount,
        "direct_cost_inr": direct_cost_inr,
        "net_pretax_receipt_inr": net_pretax_receipt,
        "contribution_margin_pct": contribution_margin_pct,
        "estimated_tax_reserve_inr": incremental_tax,
        "effective_tax_rate_pct": round(tax_calc["effective_business_tax_rate_pct"], 2),
        "net_owner_profit_inr": net_owner_profit,
        "estimated_hours": estimated_hours,
        "hourly_yield_inr": hourly_yield_inr,
    }


def get_commercial_performance_dashboard() -> dict[str, Any]:
    """
    Distinguishes the 7 exact commercial performance metrics:
    1. Qualified leads (genuine potential customers)
    2. Proposals sent (actual acquisition activity)
    3. Contracts won (confirmed work)
    4. Payments collected (actual cash received, ex-GST)
    5. Project contribution (revenue after direct costs & platform fees)
    6. Net profit after tax (real take-home earnings)
    7. Owner hours (how much operational time invested)
    Never counts an interested customer or unpaid invoice as profit.
    """
    from core.db import get_financial_states_summary, get_owner_time_summary
    from core.financial_ledger import get_cash_and_reserves_summary

    f_metrics = get_funnel_metrics()
    f_states = get_financial_states_summary()
    cash_info = get_cash_and_reserves_summary()
    time_summary = get_owner_time_summary()

    with connect() as conn:
        row_props = conn.execute("SELECT COUNT(*) FROM proposals WHERE status = 'approved_by_owner'").fetchone()
        proposals_sent = int(row_props[0] if row_props else 0)

        row_won = conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE status IN ('approved_for_work', 'submitted_by_owner', 'won', 'paid')"
        ).fetchone()
        contracts_won = int(row_won[0] if row_won else 0)

        row_repeat = conn.execute(
            """SELECT client_name, COUNT(*) as cnt FROM invoices
               WHERE status = 'paid' GROUP BY client_name HAVING cnt >= 2"""
        ).fetchall()
        repeat_customers_count = len(row_repeat)

        row_qa = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('submission_ready', 'completed')").fetchone()
        qa_passed_count = int(row_qa[0] if row_qa else 0)

    payments_collected = cash_info["collected_receipts_inr"]
    operating_profit = cash_info["operating_profit_inr"]
    net_profit = round(operating_profit - cash_info["monthly_tax_provision_inr"], 2)

    step1 = f_metrics["identified_count"] >= 5
    step2 = max(proposals_sent, f_metrics["approached_count"]) >= 5
    step3 = f_metrics["in_conversation_count"] >= 1
    step4 = f_metrics["paid_pilot_count"] >= 1 or contracts_won >= 1
    step5 = qa_passed_count >= 1
    step6 = payments_collected > 0
    step7 = time_summary["total_hours"] > 0 and cash_info["total_operating_expenses_inr"] > 0

    scoreboard_items = [
        {"id": 1, "title": "Five qualified businesses identified", "completed": step1},
        {"id": 2, "title": "Five personalized approaches sent", "completed": step2},
        {"id": 3, "title": "At least one serious customer conversation", "completed": step3},
        {"id": 4, "title": "First paid pilot secured", "completed": step4},
        {"id": 5, "title": "Deliverables independently checked", "completed": step5},
        {"id": 6, "title": "Payment confirmed against bank statement", "completed": step6},
        {"id": 7, "title": "Actual project costs and owner hours recorded", "completed": step7},
    ]
    scoreboard_completed = sum(1 for item in scoreboard_items if item["completed"])

    return {
        "qualified_leads": f_metrics["identified_count"],
        "proposals_sent": max(proposals_sent, f_metrics["approached_count"]),
        "contracts_won": contracts_won,
        "payments_collected_inr": payments_collected,
        "project_contribution_inr": operating_profit,
        "net_profit_after_tax_inr": net_profit,
        "owner_hours": time_summary["total_hours"],
        "scoreboard": {
            "score": scoreboard_completed,
            "total": 7,
            "items": scoreboard_items,
        },
        "m64_validation_targets": {
            "prospective_businesses_target": 5,
            "prospective_businesses_current": f_metrics["identified_count"],
            "serious_conversations_target": 3,
            "serious_conversations_current": f_metrics["in_conversation_count"],
            "first_paid_customer_target": 1,
            "first_paid_customer_current": 1 if f_metrics["paid_pilot_count"] >= 1 else 0,
            "completed_paid_pilots_target": 3,
            "completed_paid_pilots_current": f_metrics["paid_pilot_count"],
            "repeat_customers_target": 2,
            "repeat_customers_current": repeat_customers_count,
            "verified_project_economics": payments_collected > 0,
        },
    }
