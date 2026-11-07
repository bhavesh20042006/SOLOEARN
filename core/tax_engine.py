from __future__ import annotations

"""
SOLOEARN Indian Tax Intelligence Engine
Implements versioned Indian Income-Tax calculations under the Income-tax Act, 2025 (FY 2026-27 / AY 2027-28),
Section 202 individual slabs, Section 156 resident rebate & marginal relief, and 4% Health and Education Cess.
Also monitors GST service turnover thresholds under CBIC rules.

NOTE: This module generates planning estimates only; it does not replace a Chartered Accountant (CA) review.
"""

import math
from typing import Any

# Default parameters under Income-tax Act, 2025 (FY 2026-27)
STANDARD_CESS_RATE = 0.04
GST_SERVICE_THRESHOLD_INR = 2000000.0      # Standard threshold in most states
GST_WARNING_THRESHOLD_INR = 1800000.0      # Early alert threshold for CA review
GST_SPECIAL_CATEGORY_THRESHOLD_INR = 1000000.0  # Special category states


def compute_income_tax_india_2026(
    taxable_income: float,
    regime: str = "new_2025",
) -> dict[str, float]:
    """
    Computes estimated Indian income tax for a resident individual sole proprietor under
    Section 202 and Section 156 of the Income-tax Act, 2025 (FY 2026-27).

    Section 202 Slabs:
      0 - 4,00,000: 0%
      4,00,001 - 8,00,000: 5%
      8,00,001 - 12,00,000: 10%
      12,00,001 - 16,00,000: 15%
      16,00,001 - 20,00,000: 20%
      20,00,001 - 24,00,000: 25%
      Above 24,00,000: 30%

    Section 156 Rebate:
      100% tax rebate if taxable income <= 12,00,000.
      Marginal relief applies just above 12,00,000 such that tax payable
      does not exceed the income exceeding 12,00,000.
    """
    if taxable_income <= 0 or math.isnan(taxable_income) or math.isinf(taxable_income):
        return {
            "taxable_income": 0.0,
            "standard_tax": 0.0,
            "rebate_156": 0.0,
            "marginal_relief": 0.0,
            "tax_before_cess": 0.0,
            "cess": 0.0,
            "total_tax": 0.0,
            "effective_rate_pct": 0.0,
        }

    income = float(taxable_income)

    # 1. Compute standard slab tax
    slabs = [
        (400000.0, 0.00),
        (800000.0, 0.05),
        (1200000.0, 0.10),
        (1600000.0, 0.15),
        (2000000.0, 0.20),
        (2400000.0, 0.25),
    ]

    standard_tax = 0.0
    prev_limit = 0.0
    for limit, rate in slabs:
        if income > limit:
            standard_tax += (limit - prev_limit) * rate
            prev_limit = limit
        else:
            standard_tax += (income - prev_limit) * rate
            prev_limit = income
            break

    if income > 2400000.0:
        standard_tax += (income - 2400000.0) * 0.30

    rebate_156 = 0.0
    marginal_relief = 0.0

    # 2. Section 156 rebate & marginal relief
    if income <= 1200000.0:
        rebate_156 = standard_tax
        tax_before_cess = 0.0
    else:
        # Check marginal relief: tax cannot exceed excess income over 12,00,000
        excess_income = income - 1200000.0
        if standard_tax > excess_income:
            marginal_relief = standard_tax - excess_income
            tax_before_cess = excess_income
        else:
            tax_before_cess = standard_tax

    cess = round(tax_before_cess * STANDARD_CESS_RATE, 2)
    total_tax = round(tax_before_cess + cess, 2)
    effective_rate = round((total_tax / income) * 100, 2) if income > 0 else 0.0

    return {
        "taxable_income": round(income, 2),
        "standard_tax": round(standard_tax, 2),
        "rebate_156": round(rebate_156, 2),
        "marginal_relief": round(marginal_relief, 2),
        "tax_before_cess": round(tax_before_cess, 2),
        "cess": cess,
        "total_tax": total_tax,
        "effective_rate_pct": effective_rate,
    }


def compute_required_pretax_profit(target_posttax_annual: float = 1200000.0) -> float:
    """
    Computes the pre-tax taxable profit required to leave the specified post-tax profit,
    accounting for Section 202 slabs, Section 156 relief, and 4% cess.
    For the target of 12,00,000 post-tax, resolves to 12,73,934 pre-tax (yielding ~73,934 tax).
    """
    if target_posttax_annual <= 0:
        return 0.0

    target = float(target_posttax_annual)
    if target < 1200000.0:
        return target

    # Slabs with 4% cess:
    # Up to 12L: 0% tax (with rebate).
    # From 12L to 12,70,588: marginal relief zone (tax = excess income).
    # Beyond 12,73,933.65: standard tax in 15% slab + 4% cess applies (effective rate on incremental = 15.6%).
    # Cumulative tax up to 12L = 60,000. With cess = 62,400.
    # Tax(Y) = 62,400 + 0.156 * (Y - 1,200,000) = 0.156 * Y - 124,800.
    # Post-tax = Y - Tax(Y) = 0.844 * Y + 124,800.
    # Therefore Y = (target - 124,800) / 0.844.
    # For target = 1,200,000: Y = 1,075,200 / 0.844 = 1273933.65 -> 12,73,934!
    if target <= 1600000.0 * 0.844 + 124800.0: # within 15% slab
        return round((target - 124800.0) / 0.844, 2)

    # General continuous binary search for higher targets
    low = 1200000.0
    high = 50000000.0
    for _ in range(60):
        mid = (low + high) / 2.0
        tax_res = compute_income_tax_india_2026(mid)
        posttax = mid - tax_res["total_tax"]
        if abs(posttax - target) < 0.01:
            return round(mid, 2)
        if posttax < target:
            low = mid
        else:
            high = mid

    return round((low + high) / 2.0, 2)


def get_gst_status(total_service_turnover: float) -> dict[str, Any]:
    """
    Monitors aggregate service turnover against the statutory CBIC GST registration threshold.
    Alerts the owner when approaching or exceeding ₹20 Lakhs (with warning at ₹18 Lakhs).
    """
    turnover = max(0.0, float(total_service_turnover))
    is_exempt = turnover < GST_WARNING_THRESHOLD_INR
    is_warning = GST_WARNING_THRESHOLD_INR <= turnover < GST_SERVICE_THRESHOLD_INR
    is_mandatory = turnover >= GST_SERVICE_THRESHOLD_INR

    if is_mandatory:
        status = "CA_REVIEW_REQUIRED"
        message = (
            f"Aggregate service turnover (₹{turnover:,.2f}) has reached or exceeded the ₹20,00,000 "
            "statutory GST threshold. Immediate Chartered Accountant review is required to formalize "
            "GST registration and tax invoicing."
        )
    elif is_warning:
        status = "APPROACHING_THRESHOLD"
        message = (
            f"Aggregate turnover (₹{turnover:,.2f}) is approaching the ₹20,00,000 GST threshold "
            f"(₹{GST_SERVICE_THRESHOLD_INR - turnover:,.2f} remaining). Schedule CA review."
        )
    else:
        status = "BELOW_THRESHOLD"
        message = (
            f"Turnover (₹{turnover:,.2f}) is below the ₹20,00,000 GST registration threshold. "
            f"Headroom: ₹{GST_SERVICE_THRESHOLD_INR - turnover:,.2f}."
        )

    return {
        "turnover_inr": turnover,
        "standard_threshold_inr": GST_SERVICE_THRESHOLD_INR,
        "warning_threshold_inr": GST_WARNING_THRESHOLD_INR,
        "status": status,
        "message": message,
        "requires_ca_review": is_mandatory,
        "headroom_inr": max(0.0, GST_SERVICE_THRESHOLD_INR - turnover),
    }
