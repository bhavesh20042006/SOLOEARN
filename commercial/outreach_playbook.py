"""
SOLOEARN Outreach Playbook Module (commercial/outreach_playbook.py)
Provides programmatic access to tailored outreach templates and follow-up sequences.
"""
from __future__ import annotations

from typing import Any

OUTREACH_TARGETS: dict[str, dict[str, Any]] = {
    "snitch": {
        "company_name": "Snitch (Snitch Apparels Pvt Ltd)",
        "contact_name": "Siddharth Dungarwal",
        "contact_role": "Founder & CEO",
        "channel": "linkedin",
        "email_subject": "Snitch multi-channel sales & COD reconciliation",
        "quick_hook": (
            "Hi Siddharth, congratulations on Snitch's rapid offline store expansion across India! "
            "Quick question: when your finance and ops team consolidates weekly sales between Shopify, "
            "Pine Labs POS, and courier COD remittances, how much manual CSV wrangling is involved? "
            "Would love to share a clean automation sample."
        ),
        "full_email": (
            "Hi Siddharth,\n\n"
            "I've been following Snitch's rapid retail store expansion across Tier 1 and Tier 2 cities "
            "alongside your core Shopify storefront.\n\n"
            "Having spoken with operations leaders at scaling fashion brands, a recurring friction point "
            "at your volume is reconciling multi-channel numbers: matching Shopify order IDs against Pine "
            "Labs retail POS settlements and courier COD remittances (especially handling RTO pin-code breakdowns).\n\n"
            "I run a specialized B2B data analytics and reconciliation service for Indian D2C brands. "
            "In 24–48 hours, we turn disparate order and payout CSVs into:\n"
            "1. An automated, formula-verified Excel model (business_analysis.xlsx)\n"
            "2. A single-file offline executive dashboard showing channel-by-channel gross vs net realized revenue\n"
            "3. An independent 6-suite QA report certifying zero formula or data errors\n\n"
            "We offer a flat ₹5,000 pilot job on any single historical monthly dataset so you can evaluate the "
            "speed and quality with zero risk.\n\n"
            "Would you or Chetan be open to checking out a 2-minute interactive preview of what the finished model looks like?\n\n"
            "Best regards,\n\n"
            "Bhavesh Khullar\n"
            "Founder, SOLOEARN Data Intelligence"
        ),
        "value_follow_up": (
            "Hi Siddharth,\n\n"
            "Following up briefly on my earlier note. I put together an interactive HTML5 dashboard preview "
            "showing how multi-channel orders (Shopify vs Retail POS vs Courier COD) can be consolidated without "
            "any external SaaS subscriptions.\n\n"
            "If this could save your operations team 15+ hours of manual spreadsheet work every month, "
            "I'd be glad to run a test on your last month's data.\n\n"
            "Best,\nBhavesh"
        ),
    },
    "blue_tokai": {
        "company_name": "Blue Tokai Coffee Roasters",
        "contact_name": "Matt Chitharanjan",
        "contact_role": "Co-Founder & CEO",
        "channel": "email",
        "email_subject": "Blue Tokai cafe POS vs online subscription reconciliation",
        "quick_hook": (
            "Hi Matt, love the coffee and the rapid growth of Blue Tokai's 80+ cafes! "
            "Quick operational question: how does your ops desk manage reconciling cafe POS transactions "
            "against online bean subscriptions and B2B orders without spending dozens of hours in Excel?"
        ),
        "full_email": (
            "Hi Matt,\n\n"
            "Hope you're having a productive week.\n\n"
            "With Blue Tokai operating 80+ cafes across India while scaling your online coffee bean subscriptions, "
            "your ops team is managing a huge volume of disparate transaction streams weekly.\n\n"
            "We specialize in rapid-turnaround data reconciliation and analytics pipelines for Indian consumer brands. "
            "In 48 hours, we take raw POS exports, subscription order logs, and bank statement CSVs and deliver:\n"
            "- An automated reconciliation pipeline matching orders to cleared bank credits\n"
            "- Repeat subscription retention cohort models in Excel\n"
            "- An executive KPI dashboard requiring zero cloud software maintenance\n\n"
            "We start with a bounded, fixed-price ₹5,000 pilot so your team can test the delivery before committing to any larger engagement.\n\n"
            "Would you be open to seeing a sample report on similar retail transaction data?\n\n"
            "Warm regards,\n\n"
            "Bhavesh Khullar\n"
            "Founder, SOLOEARN Data Intelligence"
        ),
        "value_follow_up": (
            "Hi Matt,\n\n"
            "Checking in with a quick sample of the multi-channel reconciliation model we built for retail transaction data. "
            "Happy to demonstrate how it handles cafe inventory replenishment tracking in under 48 hours.\n\n"
            "Warmly,\nBhavesh"
        ),
    },
    "mokobara": {
        "company_name": "Mokobara",
        "contact_name": "Sangeet Agrawal",
        "contact_role": "Founder & CEO",
        "channel": "linkedin",
        "email_subject": "Mokobara quick commerce vs direct sales margin reconciliation",
        "quick_hook": (
            "Hi Sangeet, incredible momentum with Mokobara's airport stores and 10-minute quick commerce deliveries! "
            "Quick question: how is your finance team reconciling dark-store stock transfers and marketplace commission "
            "deductions against net bank credits?"
        ),
        "full_email": (
            "Hi Sangeet,\n\n"
            "Loved seeing Mokobara's presence expand into airport stores and 10-minute quick commerce deliveries.\n\n"
            "As quick commerce channels (Zepto/Blinkit) take off alongside your flagship D2C webstore, "
            "finance teams often struggle with margin transparency: reconciling dark-store inventory transfers "
            "against weekly payment remittance statements and commission deductions.\n\n"
            "I build automated data reconciliation pipelines for fast-growing D2C consumer brands. We deliver:\n"
            "- Reconciled settlement matrices matching dark store dispatches to bank credits\n"
            "- Reusable Python reconciliation scripts with automated test suites\n"
            "- Cleaned multi-tab Excel models with 100% deterministic formula verification\n\n"
            "We offer a flat ₹5,000 pilot on your last month's settlement exports to prove immediate value to your ops team.\n\n"
            "Would you be open to reviewing a 3-minute sample package?\n\n"
            "Best regards,\n\n"
            "Bhavesh Khullar\n"
            "Founder, SOLOEARN Data Intelligence"
        ),
        "value_follow_up": (
            "Hi Sangeet, sharing a quick screenshot preview of our dark-store inventory settlement model. "
            "Let me know if you'd like to test this on one of Mokobara's recent payout cycles.\n\nBest,\nBhavesh"
        ),
    },
    "slurrp_farm": {
        "company_name": "Slurrp Farm (Wholsum Foods)",
        "contact_name": "Meghana Narayan & Shauravi Malik",
        "contact_role": "Co-Founders",
        "channel": "email",
        "email_subject": "Slurrp Farm marketplace fee deduction audit & reconciliation",
        "quick_hook": (
            "Hi Meghana & Shauravi, congratulations on Slurrp Farm's wonderful brand growth! "
            "Managing sales across Amazon, Flipkart, FirstCry, and modern retail creates huge reconciliation overhead. "
            "We automate that entire process in 48h."
        ),
        "full_email": (
            "Dear Meghana & Shauravi,\n\n"
            "Congratulations on the incredible journey with Slurrp Farm and Wholsum Foods.\n\n"
            "Selling across Amazon, Flipkart, FirstCry, and offline retail chains means dealing with complex weekly "
            "settlement sheets with dozens of deduction line items (advertising fees, return shipping penalties, storage costs).\n\n"
            "At SOLOEARN, we provide automated multi-marketplace reconciliation for Indian FMCG & D2C brands:\n"
            "- Reconciling marketplace settlement reports against net bank credits\n"
            "- Pinpointing overcharged fulfillment or commission fees\n"
            "- Delivering clean Excel models and offline executive summaries within 48 hours\n\n"
            "We can run a pilot reconciliation on a single marketplace settlement sheet for a flat fee of ₹5,000.\n\n"
            "Would you be open to seeing how our automated audit reports look?\n\n"
            "Warm regards,\n\n"
            "Bhavesh Khullar\n"
            "Founder, SOLOEARN Data Intelligence"
        ),
        "value_follow_up": (
            "Dear Meghana & Shauravi, following up with a quick overview of how our marketplace commission fee auditor "
            "identifies unauthorized deductions. Happy to run a zero-risk pilot anytime.\n\nWarmly,\nBhavesh"
        ),
    },
    "bombay_shaving_company": {
        "company_name": "Bombay Shaving Company",
        "contact_name": "Shantanu Deshpande",
        "contact_role": "Founder & CEO",
        "channel": "linkedin",
        "email_subject": "Bombay Shaving Co. multi-channel payout reconciliation",
        "quick_hook": (
            "Hi Shantanu, huge fan of the transparency you bring to building consumer brands in India. "
            "Quick question: how many senior ops hours does your finance desk spend each week cross-checking "
            "CSV settlement dumps between web, marketplace, and distributors?"
        ),
        "full_email": (
            "Hi Shantanu,\n\n"
            "Huge fan of the content and the transparency you bring to building consumer businesses in India.\n\n"
            "Quick question from an operational angle: as Bombay Shaving Co. balances direct webstore orders, "
            "marketplace sales, and modern trade distributor accounts, how many hours does your finance desk "
            "spend each week cross-checking CSV settlement exports?\n\n"
            "I run an analytics and data operations service specifically built for Indian omnichannel brands. "
            "We take raw transaction dumps and deliver:\n"
            "1. Cleaned, deduplicated CSV datasets\n"
            "2. Complete multi-tab Excel financial models with verified formulas\n"
            "3. Standalone offline HTML5 dashboards for executive review\n"
            "4. 6-suite QA certification guaranteeing 100% data integrity\n\n"
            "We offer a flat ₹5,000 test pilot with a guaranteed 24-hour turnaround.\n\n"
            "Would you be open to letting your ops team test us with one messy dataset?\n\n"
            "Best regards,\n\n"
            "Bhavesh Khullar\n"
            "Founder, SOLOEARN Data Intelligence"
        ),
        "value_follow_up": (
            "Hi Shantanu, sharing a 1-minute look at our offline executive dashboard for multi-channel sales. "
            "Open to testing this on one of your weekly dumps?\n\nBest,\nBhavesh"
        ),
    },
}

def get_outreach_pitch(account_key: str) -> dict[str, Any] | None:
    """Returns the pitch data dictionary for a given key (e.g. 'snitch', 'blue_tokai')."""
    return OUTREACH_TARGETS.get(account_key.lower().replace("-", "_").replace(" ", "_"))

def list_all_outreach_targets() -> list[dict[str, Any]]:
    """Returns all curated target outreach profiles."""
    return list(OUTREACH_TARGETS.values())
