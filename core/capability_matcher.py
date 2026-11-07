from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class CapabilityMatchResult:
    skill_match: float  # 0.0 to 1.0
    is_suitable: bool
    matched_capabilities: list[str]
    unsupported_requirements: list[str]
    explanation: str


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


class CapabilityMatcher:
    """
    Evaluates whether an incoming job lead matches SOLOEARN's actual, verified
    automation capabilities (CSV/Excel business analytics, data cleaning, and charts)
    versus unsupported domains (mobile apps, 3D modeling, web development, etc.).
    """

    @classmethod
    def match(cls, title: str, description: str = "") -> CapabilityMatchResult:
        text = f"{title} {description}".lower()

        matched_caps: list[str] = []
        for cap_name, keywords in CORE_CAPABILITIES.items():
            if any(re.search(r"\b" + re.escape(kw) + r"\b", text) for kw in keywords):
                matched_caps.append(cap_name)

        unsupported_found: list[str] = [
            kw for kw in UNSUPPORTED_KEYWORDS
            if re.search(r"\b" + re.escape(kw) + r"\b", text)
        ]

        # Scoring logic. Any unsupported requirement forces manual review/rejection;
        # matching one supported subtask must never make an out-of-scope project suitable.
        if unsupported_found:
            score = min(0.2, round(len(matched_caps) * 0.04, 2))
            explanation = (
                "Unsupported requirement(s) detected; the opportunity is not eligible for automatic execution: "
                f"{', '.join(unsupported_found[:4])}."
            )
            is_suitable = False
        elif matched_caps:
            # Clean match on verified automation capabilities
            base_score = 0.65
            bonus = min(0.35, len(matched_caps) * 0.08)
            score = round(min(1.0, base_score + bonus), 2)
            readable_caps = [c.replace("_", " ").title() for c in matched_caps]
            explanation = f"Strong match with verified SOLOEARN capabilities: {', '.join(readable_caps)}."
            is_suitable = True
        else:
            # Generic or ambiguous job
            score = 0.4
            explanation = "General task with no explicit CSV, Excel, or tabular analytics keywords detected."
            is_suitable = False

        return CapabilityMatchResult(
            skill_match=score,
            is_suitable=is_suitable,
            matched_capabilities=matched_caps,
            unsupported_requirements=unsupported_found,
            explanation=explanation,
        )
