from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapabilityMatchResult:
    skill_match: float  # 0.0 to 1.0
    is_suitable: bool
    matched_capabilities: list[str]
    unsupported_requirements: list[str]
    explanation: str
    industry_fit: float = 0.5
    detected_industry: str = ""
    tech_stack_fit: float = 0.5
    detected_tech_stack: list[str] = field(default_factory=list)
    pain_point_fit: float = 0.5
    detected_pain_points: list[str] = field(default_factory=list)
    overall_fit_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_match": self.skill_match,
            "is_suitable": self.is_suitable,
            "matched_capabilities": list(self.matched_capabilities),
            "unsupported_requirements": list(self.unsupported_requirements),
            "explanation": self.explanation,
            "industry_fit": self.industry_fit,
            "detected_industry": self.detected_industry,
            "tech_stack_fit": self.tech_stack_fit,
            "detected_tech_stack": list(self.detected_tech_stack),
            "pain_point_fit": self.pain_point_fit,
            "detected_pain_points": list(self.detected_pain_points),
            "overall_fit_score": self.overall_fit_score,
        }


# SOLOEARN Verified Automation Capabilities
CORE_CAPABILITIES: dict[str, list[str]] = {
    "csv_data_cleaning": [
        "csv", "data clean", "data cleaning", "clean data", "deduplicate", "deduplication",
        "remove duplicates", "missing data", "data quality", "profile data", "sanitize data"
    ],
    "excel_compilation": [
        "excel", "xlsx", "spreadsheet", "workbook", "vlookup", "pivot", "formulas",
        "excel dashboard", "format spreadsheet", "excel report"
    ],
    "business_analytics": [
        "business analysis", "sales analysis", "kpi", "metrics", "financial analysis",
        "revenue analysis", "retail data", "trend analysis", "statistical summary", "descriptive statistics"
    ],
    "chart_visualization": [
        "charts", "graphs", "visualizations", "distribution plot", "histogram",
        "bar chart", "data visual", "visual analytics"
    ],
    "executive_reporting": [
        "html report", "executive report", "summary presentation", "executive summary",
        "client presentation", "business report", "management report"
    ],
}

# Tasks outside SOLOEARN's automated capabilities in v0.1 - v0.4
UNSUPPORTED_KEYWORDS: list[str] = [
    "mobile app", "ios", "android", "flutter", "react native", "swift", "kotlin",
    "wordpress", "shopify store", "fullstack", "react.js", "next.js", "vue", "angular",
    "video edit", "video editing", "premiere pro", "after effects", "animation",
    "3d model", "blender", "cad", "solidworks", "3d rendering",
    "voiceover", "audio editing", "podcast edit", "music production",
    "translation", "transcription", "copywriting", "seo blog", "ghostwriting",
    "penetration test", "cybersecurity audit", "smart contract", "solidity", "web3"
]

# Industry Fit Scoring Matrix for Indian D2C, E-commerce, Retail & SMBs
INDUSTRY_FIT_WEIGHTS: dict[str, float] = {
    "ecommerce": 1.0,
    "e-commerce": 1.0,
    "d2c": 1.0,
    "direct-to-consumer": 1.0,
    "retail": 1.0,
    "fmcg": 0.95,
    "apparel": 0.95,
    "fashion": 0.95,
    "food_beverage": 0.95,
    "food": 0.95,
    "beverage": 0.95,
    "beauty_wellness": 0.95,
    "personal_care": 0.95,
    "wholesale": 0.90,
    "accounting": 0.90,
    "logistics": 0.85,
    "supply_chain": 0.85,
    "consumer_goods": 0.95,
    "services": 0.80,
    "technology": 0.65,
    "saas": 0.65,
    "manufacturing": 0.60,
    "healthcare": 0.60,
    "education": 0.50,
    "other": 0.50,
}

DISALLOWED_OR_HIGH_RISK_INDUSTRIES: set[str] = {
    "crypto",
    "cryptocurrency",
    "gambling",
    "betting",
    "casino",
    "mlm",
    "network_marketing",
    "adult",
    "unregulated_lending",
}

# E-commerce, ERP, Courier & Payment Tech Stack Signatures
TECH_STACK_SIGNATURES: dict[str, list[str]] = {
    "shopify": ["shopify", "shopify plus"],
    "amazon_seller": ["amazon", "amazon seller central", "seller central", "amazon fba", "seller flex", "amazon pi"],
    "flipkart_seller": ["flipkart", "flipkart seller hub", "flipkart assured", "fbf"],
    "quick_commerce": ["blinkit", "zepto", "instamart", "swiggy instamart", "bb now", "bigbasket"],
    "ondc": ["ondc", "open network for digital commerce"],
    "woocommerce": ["woocommerce", "woo commerce"],
    "magento": ["magento", "adobe commerce"],
    "unicommerce": ["unicommerce", "uniware"],
    "easyecom": ["easyecom"],
    "vinculum": ["vinculum", "vin eretail"],
    "increff": ["increff"],
    "shiprocket": ["shiprocket", "shiprocket omuni"],
    "delhivery": ["delhivery", "delhivery one"],
    "clickpost": ["clickpost"],
    "pickrr": ["pickrr"],
    "tally": ["tally", "tallyprime", "tally prime", "tally erp"],
    "zoho": ["zoho books", "zoho inventory", "zoho"],
    "busy": ["busy", "busy accounting"],
    "razorpay": ["razorpay", "razorpayx"],
    "cashfree": ["cashfree"],
    "payu": ["payu"],
    "excel_spreadsheet": ["excel", "xlsx", "google sheets", "spreadsheet"],
}

# High-Fit Indian D2C & SMB Pain Point Patterns
PAIN_POINT_PATTERNS: dict[str, list[str]] = {
    "settlement_reconciliation": [
        "reconciliation", "reconcile", "settlement", "payout mismatch",
        "payment gateway mismatch", "bank reconciliation", "gateway reconciliation",
        "cod remittance", "cod reconciliation", "payout reconciliation"
    ],
    "rto_cod_leakage": [
        "rto", "return to origin", "non delivery report", "ndr",
        "rto loss", "courier return", "undelivered orders", "return reconciliation"
    ],
    "marketplace_deduction_audit": [
        "weight discrepancy", "excess weight", "commission deduction",
        "fee audit", "marketplace leakage", "commission clawback",
        "shipping overcharges", "ad spend deduction", "marketplace deductions"
    ],
    "inventory_stock_velocity": [
        "inventory sync", "stockout", "dead stock", "sku velocity",
        "stock aging", "inventory turnover", "multi-channel inventory", "stock reconciliation"
    ],
    "retention_cohort_analytics": [
        "cohort", "repeat rate", "customer retention", "churn",
        "subscriber churn", "reorder rate", "aov", "customer ltv", "cac", "repeat purchase"
    ],
    "distributor_b2b_claims": [
        "distributor claims", "credit note", "debit memo",
        "modern trade deductions", "b2b billing", "claim reconciliation", "distributor reconciliation"
    ],
}


class CapabilityMatcher:
    """
    Evaluates whether an incoming job lead matches SOLOEARN's actual, verified
    automation capabilities (CSV/Excel business analytics, data cleaning, and charts)
    versus unsupported domains (mobile apps, 3D modeling, web development, etc.).

    Enhanced with Industry, Tech Stack, and Operational Pain-Point scoring tailored
    for Indian D2C brands, e-commerce platforms, and SMB retailers.
    """

    @classmethod
    def match(
        cls,
        title: str,
        description: str = "",
        *,
        industry: str = "",
        tech_stack: list[str] | str | None = None,
        pain_points: list[str] | str | None = None,
    ) -> CapabilityMatchResult:
        combined_text = f"{title} {description}".lower()

        # 1. Core Capability Detection
        matched_caps: list[str] = []
        for cap_name, keywords in CORE_CAPABILITIES.items():
            if any(re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in keywords):
                matched_caps.append(cap_name)

        # 2. Unsupported Requirement Detection
        unsupported_found: list[str] = [
            kw for kw in UNSUPPORTED_KEYWORDS
            if re.search(r"\b" + re.escape(kw) + r"\b", combined_text)
        ]

        # 3. Industry Detection & Fit
        detected_ind = industry.strip().lower() if industry else ""
        if not detected_ind:
            for ind_key in INDUSTRY_FIT_WEIGHTS:
                if re.search(r"\b" + re.escape(ind_key) + r"\b", combined_text):
                    detected_ind = ind_key
                    break
        if not detected_ind:
            detected_ind = "other"

        industry_fit = INDUSTRY_FIT_WEIGHTS.get(detected_ind, 0.5)

        # Check for disallowed/high-risk industries
        is_disallowed_industry = (
            detected_ind in DISALLOWED_OR_HIGH_RISK_INDUSTRIES
            or any(re.search(r"\b" + re.escape(bad) + r"\b", combined_text) for bad in DISALLOWED_OR_HIGH_RISK_INDUSTRIES)
        )

        # 4. Tech Stack Detection & Fit
        detected_stacks: list[str] = []
        if tech_stack:
            if isinstance(tech_stack, str):
                ts_list = [s.strip().lower() for s in re.split(r"[,;|]+", tech_stack) if s.strip()]
            else:
                ts_list = [str(s).strip().lower() for s in tech_stack if str(s).strip()]
            detected_stacks.extend(ts_list)
        else:
            for stack_key, keywords in TECH_STACK_SIGNATURES.items():
                if any(re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in keywords):
                    detected_stacks.append(stack_key)

        # Calculate tech stack fit score
        if detected_stacks:
            # Reward multi-channel / D2C tech stacks
            high_value_stacks = {"shopify", "amazon_seller", "flipkart_seller", "quick_commerce", "ondc", "unicommerce", "easyecom", "tally", "zoho", "delhivery", "shiprocket"}
            matched_high_value = [s for s in detected_stacks if s in high_value_stacks]
            tech_stack_fit = min(1.0, 0.60 + 0.10 * len(matched_high_value) + 0.05 * len(detected_stacks))
        else:
            tech_stack_fit = 0.50

        # 5. Pain Point Detection & Fit
        detected_pain_points: list[str] = []
        if pain_points:
            if isinstance(pain_points, str):
                pp_list = [p.strip().lower() for p in re.split(r"[,;|]+", pain_points) if p.strip()]
            else:
                pp_list = [str(p).strip().lower() for p in pain_points if str(p).strip()]
            detected_pain_points.extend(pp_list)
        else:
            for pp_key, keywords in PAIN_POINT_PATTERNS.items():
                if any(re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in keywords):
                    detected_pain_points.append(pp_key)

        if detected_pain_points:
            pain_point_fit = min(1.0, 0.65 + 0.10 * len(detected_pain_points))
        else:
            pain_point_fit = 0.50

        # 6. Overall Suitability & Fit Score Synthesis
        if unsupported_found or is_disallowed_industry:
            score = min(0.2, round(len(matched_caps) * 0.04, 2))
            reasons: list[str] = []
            if unsupported_found:
                reasons.append(f"unsupported requirement(s): {', '.join(unsupported_found[:4])}")
            if is_disallowed_industry:
                reasons.append(f"disallowed/high-risk industry '{detected_ind}'")
            explanation = (
                f"Opportunity is not eligible for automatic execution due to {'; '.join(reasons)}."
            )
            is_suitable = False
            overall_fit = score
        elif matched_caps:
            base_score = 0.65
            bonus = min(0.35, len(matched_caps) * 0.08)
            score = round(min(1.0, base_score + bonus), 2)
            readable_caps = [c.replace("_", " ").title() for c in matched_caps]
            
            # Composite overall fit: 40% capability, 25% pain points, 20% tech stack, 15% industry
            overall_fit = round(
                0.40 * score + 0.25 * pain_point_fit + 0.20 * tech_stack_fit + 0.15 * industry_fit,
                2,
            )
            explanation_parts = [f"Strong match with verified SOLOEARN capabilities: {', '.join(readable_caps)}"]
            if detected_stacks:
                explanation_parts.append(f"Tech Stack: {', '.join(detected_stacks)}")
            if detected_pain_points:
                readable_pps = [p.replace("_", " ").title() for p in detected_pain_points]
                explanation_parts.append(f"Pain Points: {', '.join(readable_pps)}")
            explanation = ". ".join(explanation_parts) + "."
            is_suitable = True
        else:
            score = 0.4
            explanation = "General task with no explicit CSV, Excel, or tabular analytics keywords detected."
            is_suitable = False
            overall_fit = 0.4

        return CapabilityMatchResult(
            skill_match=score,
            is_suitable=is_suitable,
            matched_capabilities=matched_caps,
            unsupported_requirements=unsupported_found,
            explanation=explanation,
            industry_fit=round(industry_fit, 2),
            detected_industry=detected_ind,
            tech_stack_fit=round(tech_stack_fit, 2),
            detected_tech_stack=detected_stacks,
            pain_point_fit=round(pain_point_fit, 2),
            detected_pain_points=detected_pain_points,
            overall_fit_score=overall_fit,
        )
