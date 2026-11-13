from __future__ import annotations

"""
SOLOEARN Indian Tax Intelligence Engine (v0.6.1.2 Hardened)
Implements versioned Indian Income-Tax calculations under the Income-tax Act, 2025 (FY 2026-27 / AY 2027-28):
- Section 202 individual slabs.
- Section 156 resident rebate & marginal relief.
- Surcharge bands (10% > ₹50L, 15% > ₹1Cr, 25% > ₹2Cr) and surcharge marginal relief.
- 4% Health and Education Cess.
- Integration of other declared personal taxable income to compute incremental business tax.
- GST service turnover monitoring under CBIC rules (₹20L common threshold, ₹18L warning).

NOTE: This module generates planning estimates only; it does not replace a Chartered Accountant (CA) review.
"""

import math
from typing import Any

STANDARD_CESS_RATE = 0.04
GST_SERVICE_THRESHOLD_INR = 2000000.0      # Standard threshold in most states
GST_WARNING_THRESHOLD_INR = 1800000.0      # Early alert threshold for CA review
GST_SPECIAL_CATEGORY_THRESHOLD_INR = 1000000.0  # Special category states

SUPPORTED_TAX_YEAR = "2026-27"
SUPPORTED_TAX_REGIME = "new_2025"
SUPPORTED_ENTITY_TYPE = "individual_sole_proprietor"
SUPPORTED_ACCOUNTING_METHODS = {"cash", "mercantile"}  # Income-tax Act, 2025 Section 276
TDS_STATUTORY_SECTION = "Section 393 (formerly Section 194J/194C)"

# Statutory TDS schedules under Income-tax Act, 2025 Section 393(1) Table 6
# Rates depend on the nature of payment and the legal status of the payee:
SECTION_393_TDS_SCHEDULE: dict[str, dict[str, Any]] = {
    "professional_services_10": {
        "label": "Professional Services (Table 6(iii)) - Standard Rate (10%)",
        "rate": 0.10,
        "payee_type": "all_payees",
        "description": "Standard rate under Section 393(1) Table 6(iii) for professional/consulting fees",
    },
    "technical_services_2": {
        "label": "Technical Services / BPO / Call Center (Table 6) - Specified Rate (2%)",
        "rate": 0.02,
        "payee_type": "all_payees",
        "description": "Specified technical and support services under Section 393 schedule",
    },
    "contractor_individual_payee_2": {
        "label": "Works Contract (Table 6(i)) - Payee is Resident Individual / HUF (2%)",
        "rate": 0.02,
        "payee_type": "individual_or_huf",
        "description": "Section 393(1) Table 6(i): Payee is individual/HUF (e.g. Solo Proprietor) -> 2%",
    },
    "contractor_other_payee_1": {
        "label": "Works Contract (Table 6(i)) - Payee is other than Individual / HUF (1%)",
        "rate": 0.01,
        "payee_type": "other_than_individual",
        "description": "Section 393(1) Table 6(i): Payee is Company, LLP, or Firm -> 1%",
    },
    "no_tds_0": {
        "label": "No TDS Deducted (0% / Sub-threshold / Form 13 Nil Certificate)",
        "rate": 0.0,
        "payee_type": "any",
        "description": "Payment below statutory threshold or under valid nil-deduction certificate",
    },
}


def inr_to_paise(amount_inr: float | int) -> int:
    """Converts a floating-point or numeric INR amount into exact integer paise."""
    if isinstance(amount_inr, bool):
        raise ValueError("Amount cannot be a boolean")
    val = float(amount_inr)
    if math.isnan(val) or math.isinf(val):
        raise ValueError("Invalid non-finite currency amount")
    return int(round(val * 100))


def paise_to_inr(paise: int) -> float:
    """Converts exact integer paise back into INR float rounded to 2 decimals."""
    return round(paise / 100.0, 2)


def calculate_section_393_tds(subtotal_inr: float, category: str) -> float:
    """Calculates the statutory Section 393 TDS amount for a given invoice subtotal and category in exact integer paise."""
    if category not in SECTION_393_TDS_SCHEDULE:
        raise ValueError(f"Invalid Section 393 category: '{category}'. Valid: {list(SECTION_393_TDS_SCHEDULE)}")
    rate = float(SECTION_393_TDS_SCHEDULE[category]["rate"])
    subtotal_paise = max(0, inr_to_paise(subtotal_inr))
    tds_paise = int(round(subtotal_paise * rate))
    return paise_to_inr(tds_paise)


def validate_tax_profile(profile: dict[str, Any] | None) -> dict[str, str]:
    """Fail closed for taxpayer configurations the current estimate engine does not model."""
    if not profile:
        return {
            "entity_type": SUPPORTED_ENTITY_TYPE,
            "residential_status": "resident",
            "tax_year": SUPPORTED_TAX_YEAR,
            "regime": SUPPORTED_TAX_REGIME,
            "accounting_method": "cash",
        }
    expected = {
        "entity_type": SUPPORTED_ENTITY_TYPE,
        "residential_status": "resident",
        "tax_year": SUPPORTED_TAX_YEAR,
        "regime": SUPPORTED_TAX_REGIME,
    }
    for key, supported_value in expected.items():
        actual = str(profile.get(key, supported_value)).strip().lower()
        if actual != supported_value.lower():
            raise ValueError(
                f"SOLOEARN tax estimates currently support only {key}={supported_value!r}; "
                f"the saved profile contains {actual!r}. Update the profile or obtain CA review."
            )
    method = str(profile.get("accounting_method", "cash")).strip().lower()
    if method not in SUPPORTED_ACCOUNTING_METHODS:
        raise ValueError(
            f"Unsupported accounting method {method!r}. Allowed: {SUPPORTED_ACCOUNTING_METHODS} "
            "(Income-tax Act, 2025 Section 276)."
        )
    return {**expected, "accounting_method": method}

# Surcharge thresholds & base benchmarks under Section 202 default regime
SURCHARGE_THRESHOLDS = [
    (5000000.0, 0.10, 1080000.0),    # Threshold 1: 50L, 10% rate, tax on 50L = 10.80L
    (10000000.0, 0.15, 2838000.0),   # Threshold 2: 1Cr, 15% rate, tax+surcharge on 1Cr = 28.38L
    (20000000.0, 0.25, 6417000.0),   # Threshold 3: 2Cr, 25% rate, tax+surcharge on 2Cr = 64.17L
]


def _compute_standard_slab_tax(income: float) -> float:
    """Computes basic slab tax under Section 202 before rebate or cess."""
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

    return round(standard_tax, 2)


def compute_surcharge(tax_base: float, total_income: float) -> tuple[float, float, float]:
    """
    Computes surcharge and marginal relief for higher incomes:
    Returns (surcharge_amount, marginal_relief_surcharge, rate_pct).
    """
    if total_income <= 5000000.0 or tax_base <= 0:
        return 0.0, 0.0, 0.0

    if total_income <= 10000000.0:
        rate = 0.10
        nom_surcharge = round(tax_base * rate, 2)
        # Cap: Tax on 50L + (Income - 50L)
        capped = 1080000.0 + (total_income - 5000000.0)
        current = tax_base + nom_surcharge
        if current > capped:
            relief = round(current - capped, 2)
            actual_surcharge = max(0.0, round(capped - tax_base, 2))
            return actual_surcharge, relief, 10.0
        return nom_surcharge, 0.0, 10.0

    elif total_income <= 20000000.0:
        rate = 0.15
        nom_surcharge = round(tax_base * rate, 2)
        # Cap: Tax+Surcharge on 1Cr + (Income - 1Cr)
        capped = 2838000.0 + (total_income - 10000000.0)
        current = tax_base + nom_surcharge
        if current > capped:
            relief = round(current - capped, 2)
            actual_surcharge = max(0.0, round(capped - tax_base, 2))
            return actual_surcharge, relief, 15.0
        return nom_surcharge, 0.0, 15.0

    else:
        rate = 0.25
        nom_surcharge = round(tax_base * rate, 2)
        # Cap: Tax+Surcharge on 2Cr + (Income - 2Cr)
        capped = 6417000.0 + (total_income - 20000000.0)
        current = tax_base + nom_surcharge
        if current > capped:
            relief = round(current - capped, 2)
            actual_surcharge = max(0.0, round(capped - tax_base, 2))
            return actual_surcharge, relief, 25.0
        return nom_surcharge, 0.0, 25.0


def compute_income_tax_india_2026(
    taxable_income: float,
    regime: str = "new_2025",
) -> dict[str, float]:
    """
    Computes estimated Indian income tax for a resident individual sole proprietor under
    Section 202, Section 156 (rebate/marginal relief), surcharge bands, and 4% cess.
    """
    regime_norm = str(regime).strip().lower()
    if regime_norm not in {SUPPORTED_TAX_REGIME.lower(), "new_2026"}:
        raise ValueError(
            f"Unsupported tax regime {regime!r}; this estimate engine currently supports "
            f"only {SUPPORTED_TAX_REGIME!r} (or 'new_2026')."
        )
    if isinstance(taxable_income, bool):
        raise ValueError("Taxable income must be numeric, not boolean")
    if taxable_income <= 0 or math.isnan(taxable_income) or math.isinf(taxable_income):
        return {
            "taxable_income": 0.0,
            "standard_tax": 0.0,
            "rebate_156": 0.0,
            "marginal_relief": 0.0,
            "tax_base_before_surcharge": 0.0,
            "surcharge": 0.0,
            "marginal_relief_surcharge": 0.0,
            "surcharge_rate_pct": 0.0,
            "tax_before_cess": 0.0,
            "cess": 0.0,
            "total_tax": 0.0,
            "effective_rate_pct": 0.0,
        }

    income = float(taxable_income)
    standard_tax = _compute_standard_slab_tax(income)

    rebate_156 = 0.0
    marginal_relief = 0.0

    # Section 156 rebate & marginal relief
    if income <= 1200000.0:
        rebate_156 = standard_tax
        tax_base = 0.0
    else:
        excess_income = income - 1200000.0
        if standard_tax > excess_income:
            marginal_relief = standard_tax - excess_income
            tax_base = excess_income
        else:
            tax_base = standard_tax

    # Surcharge & marginal relief on surcharge
    surcharge, surcharge_relief, surcharge_rate = compute_surcharge(tax_base, income)
    tax_before_cess_paise = inr_to_paise(tax_base) + inr_to_paise(surcharge)
    tax_before_cess = paise_to_inr(tax_before_cess_paise)

    cess_paise = int(round(tax_before_cess_paise * STANDARD_CESS_RATE))
    cess = paise_to_inr(cess_paise)
    total_tax_paise = tax_before_cess_paise + cess_paise
    total_tax = paise_to_inr(total_tax_paise)
    effective_rate = round((total_tax / income) * 100.0, 2) if income > 0 else 0.0

    return {
        "taxable_income": round(income, 2),
        "standard_tax": round(standard_tax, 2),
        "rebate_156": round(rebate_156, 2),
        "marginal_relief": round(marginal_relief, 2),
        "tax_base_before_surcharge": round(tax_base, 2),
        "surcharge": surcharge,
        "marginal_relief_surcharge": surcharge_relief,
        "surcharge_rate_pct": surcharge_rate,
        "tax_before_cess": tax_before_cess,
        "cess": cess,
        "total_tax": total_tax,
        "effective_rate_pct": effective_rate,
        "tax_before_cess_paise": tax_before_cess_paise,
        "cess_paise": cess_paise,
        "total_tax_paise": total_tax_paise,
    }


def compute_business_incremental_tax(
    business_taxable_income: float,
    other_annual_income: float = 0.0,
    regime: str = SUPPORTED_TAX_REGIME,
) -> dict[str, float]:
    """
    Computes the incremental income tax directly attributable to business income,
    accounting for other declared personal taxable income with exact paise precision.
    """
    b_income = max(0.0, float(business_taxable_income))
    o_income = max(0.0, float(other_annual_income))
    tot_income = b_income + o_income

    tax_total = compute_income_tax_india_2026(tot_income, regime=regime)
    tax_other = compute_income_tax_india_2026(o_income, regime=regime)
    inc_tax_paise = max(0, inr_to_paise(tax_total["total_tax"]) - inr_to_paise(tax_other["total_tax"]))
    inc_tax = paise_to_inr(inc_tax_paise)

    return {
        "business_taxable_income": b_income,
        "other_annual_income": o_income,
        "total_taxable_income": tot_income,
        "tax_on_total": tax_total["total_tax"],
        "tax_on_other": tax_other["total_tax"],
        "incremental_business_tax": inc_tax,
        "effective_business_tax_rate_pct": round((inc_tax / b_income) * 100.0, 2) if b_income > 0 else 0.0,
    }


def compute_required_pretax_profit(
    target_posttax_annual: float = 1200000.0,
    other_annual_income: float = 0.0,
    regime: str = SUPPORTED_TAX_REGIME,
) -> float:
    """
    Computes the taxable business profit required to yield `target_posttax_annual`
    after attributable income tax, taking into account other personal income.
    """
    if str(regime).strip() != SUPPORTED_TAX_REGIME:
        raise ValueError(
            f"Unsupported tax regime {regime!r}; this estimate engine currently supports "
            f"only {SUPPORTED_TAX_REGIME!r}."
        )
    if isinstance(target_posttax_annual, bool) or not math.isfinite(float(target_posttax_annual)):
        raise ValueError("Target post-tax income must be a finite numeric amount")
    if target_posttax_annual <= 0:
        return 0.0

    target = float(target_posttax_annual)
    other = max(0.0, float(other_annual_income))

    # Analytical fast check for standard ₹12,00,000 target
    if other == 0.0 and abs(target - 1200000.0) < 0.01:
        return 1273933.65

    # Fast analytical check if other == 0 and target strictly below ₹12,00,000 (zero tax due to Sec 156 rebate)
    if other == 0.0 and target < 1200000.0:
        return target

    # Monotonic binary search solver
    low = 0.0
    high = max(50000000.0, (target + other) * 3.0)
    for _ in range(70):
        mid = (low + high) / 2.0
        inc_res = compute_business_incremental_tax(mid, other, regime=regime)
        posttax = mid - inc_res["incremental_business_tax"]
        if abs(posttax - target) < 0.001:
            return round(mid, 2)
        if posttax < target:
            low = mid
        else:
            high = mid

    return round((low + high) / 2.0, 2)


def get_gst_status(
    total_service_turnover: float,
    threshold_inr: float = GST_SERVICE_THRESHOLD_INR,
) -> dict[str, Any]:
    """
    Monitors aggregate service turnover against the applicable CBIC GST registration threshold.
    Supports standard state threshold (₹20 Lakhs) or special category states (₹10 Lakhs).
    Alerts the owner when approaching (90% of threshold) or exceeding the limit.
    """
    turnover = max(0.0, float(total_service_turnover))
    thresh = max(100000.0, float(threshold_inr))
    warn_thresh = 0.90 * thresh

    is_warning = warn_thresh <= turnover < thresh
    is_mandatory = turnover >= thresh

    if is_mandatory:
        status = "CA_REVIEW_REQUIRED"
        message = (
            f"Aggregate same-PAN service turnover (₹{turnover:,.2f}) has reached or exceeded the ₹{thresh:,.0f} "
            "statutory GST threshold assumption. Immediate Chartered Accountant review is required to formalize "
            "GST registration and tax invoicing. Registration is not automatic and tax cannot be charged until registered."
        )
    elif is_warning:
        status = "APPROACHING_THRESHOLD"
        message = (
            f"Aggregate turnover (₹{turnover:,.2f}) is approaching the ₹{thresh:,.0f} statutory threshold "
            f"(₹{thresh - turnover:,.2f} remaining). Schedule CA review before accepting further contracts."
        )
    else:
        status = "BELOW_THRESHOLD"
        message = (
            f"Turnover (₹{turnover:,.2f}) is below the ₹{thresh:,.0f} registration threshold assumption. "
            f"Headroom: ₹{thresh - turnover:,.2f}."
        )

    return {
        "turnover_inr": turnover,
        "standard_threshold_inr": thresh,
        "warning_threshold_inr": warn_thresh,
        "status": status,
        "message": message,
        "requires_ca_review": is_mandatory,
        "headroom_inr": max(0.0, thresh - turnover),
    }
