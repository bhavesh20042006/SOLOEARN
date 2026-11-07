from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class RiskAnalysisResult:
    risk_level: float  # 1.0 (safest) to 10.0 (highest risk)
    payment_confidence: float  # 0.0 to 1.0
    is_suspicious: bool
    risk_flags: list[str]
    summary: str


SUSPICIOUS_COMMUNICATION_PATTERNS: list[tuple[str, str]] = [
    (r"\b(telegram|t\.me)\b", "Directs communication to Telegram (common freelance scam pattern)"),
    (r"\b(whatsapp|wa\.me)\b", "Directs communication to WhatsApp off-platform"),
    (r"\bcontact\s+(me\s+)?(at|on|via)\s+[a-zA-Z0-9._%+-]+@\w+", "Attempts immediate off-platform contact bypass"),
    (r"\bskype\s*:\s*[a-zA-Z0-9_]+", "Requests communication via Skype"),
]

FINANCIAL_FRAUD_PATTERNS: list[tuple[str, str]] = [
    (r"\b(gift\s*card|steam\s*card|itunes\s*card|amazon\s*card)\b", "Mentions gift card payment (known payment scam)"),
    (r"\b(western\s*union|moneygram)\b", "Requests wire transfer via Western Union or MoneyGram"),
    (r"\b(crypto|bitcoin|usdt|eth|ethereum|binance)\b", "Mentions cryptocurrency or unregulated digital assets"),
    (r"\b(cheque|check)\s+(deposit|clearing|advance)\b", "Mentions check/cheque deposit scheme (counterfeit check risk)"),
    (r"\b(security\s*deposit|upfront\s*fee|registration\s*fee)\b", "Requires worker to pay an upfront or registration fee"),
    (r"\b(free\s*test\s*task|unpaid\s*trial\s*work)\b", "Demands substantial unpaid test work before hiring"),
]

SUSPICIOUS_TLDS = {".xyz", ".top", ".tk", ".ml", ".ga", ".cf", ".gq", ".click", ".link", ".buzz", ".monster"}


class RiskAnalyzer:
    """
    Evaluates freelance and client opportunities for fraud, off-platform communication bypass,
    financial scams, rate anomalies, and link safety.
    """

    @classmethod
    def analyze(
        cls,
        title: str,
        description: str = "",
        amount_inr: float = 0.0,
        estimated_hours: float = 1.0,
        url: str = "",
        source_permitted: bool = True,
    ) -> RiskAnalysisResult:
        text = f"{title} {description}".lower()
        flags: list[str] = []

        base_risk = 2.0
        confidence = 0.85

        # 1. Source authorization
        if not source_permitted:
            flags.append("Opportunity comes from an unpermitted or unauthorized source.")
            base_risk += 4.0
            confidence -= 0.4

        # 2. Communication channels
        for pattern, explanation in SUSPICIOUS_COMMUNICATION_PATTERNS:
            if re.search(pattern, text):
                flags.append(explanation)
                base_risk += 2.5
                confidence -= 0.25

        # 3. Financial fraud indicators
        for pattern, explanation in FINANCIAL_FRAUD_PATTERNS:
            if re.search(pattern, text):
                flags.append(explanation)
                base_risk += 3.5
                confidence -= 0.35

        # 4. Financial rate sanity checks
        hrs = max(0.5, float(estimated_hours))
        amt = float(amount_inr)
        if amt <= 0:
            flags.append("Budget is zero or unspecified.")
            base_risk += 1.5
            confidence -= 0.2
        else:
            rate_per_hour = amt / hrs
            if rate_per_hour > 35000:
                flags.append(f"Unusually high hourly rate (₹{rate_per_hour:,.0f}/hr) for entry-level tasks.")
                base_risk += 2.0
                confidence -= 0.15
            elif rate_per_hour < 100:
                flags.append(f"Extremely low compensation (₹{rate_per_hour:,.0f}/hr).")
                base_risk += 1.0

        # 5. Link & URL safety checks
        if url:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if any(domain.endswith(tld) for tld in SUSPICIOUS_TLDS):
                flags.append(f"URL domain ends in high-risk TLD: {domain}")
                base_risk += 2.0
                confidence -= 0.2
            # Check for direct IP address URLs
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$", domain):
                flags.append("URL netloc is a raw IP address instead of a domain name.")
                base_risk += 3.0
                confidence -= 0.3

        # Clamp boundaries
        final_risk = round(min(10.0, max(1.0, base_risk)), 1)
        final_confidence = round(max(0.05, min(0.95, confidence)), 2)
        is_suspicious = final_risk >= 5.5 or len(flags) >= 2

        summary = (
            f"Flagged {len(flags)} risk factor(s). High suspicion."
            if is_suspicious
            else (f"{len(flags)} minor risk warning(s)." if flags else "No suspicious risk indicators detected.")
        )

        return RiskAnalysisResult(
            risk_level=final_risk,
            payment_confidence=final_confidence,
            is_suspicious=is_suspicious,
            risk_flags=flags,
            summary=summary,
        )
