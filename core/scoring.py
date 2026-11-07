from __future__ import annotations


def opportunity_score(amount_inr: float, estimated_hours: float, skill_match: float,
                      payment_confidence: float, risk_level: float, source_permitted: bool) -> float:
    """Expected risk-adjusted INR/hour, not a guarantee of receiving payment."""
    if estimated_hours <= 0:
        raise ValueError("Estimated hours must be greater than zero")
    if amount_inr < 0:
        raise ValueError("Amount cannot be negative")
    for name, value in (("skill_match", skill_match), ("payment_confidence", payment_confidence)):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if not 0 <= risk_level <= 10:
        raise ValueError("risk_level must be between 0 and 10")
    if not source_permitted:
        return 0.0
    risk_factor = max(0.1, 1 - (risk_level / 12))
    return round((amount_inr / estimated_hours) * skill_match * payment_confidence * risk_factor, 2)


def score_label(score: float) -> str:
    if score >= 1500:
        return "High potential — verify carefully"
    if score >= 500:
        return "Worth reviewing"
    if score > 0:
        return "Low expected return"
    return "Not eligible / not permitted"
