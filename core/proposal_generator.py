from __future__ import annotations

from typing import Any, Callable

from core.capability_matcher import CapabilityMatcher
from core.db import get_opportunity, get_proposal, log_event, save_proposal_draft, update_proposal_status


PROPOSAL_SYSTEM_PROMPT = """You are a professional, highly disciplined freelance business data analyst representing SOLOEARN.
Your job is to generate a concise, tailored, and credible client proposal for a data analysis opportunity.

Strict constraints:
1. Ground your proposal strictly in verified data-analytics capabilities: CSV cleaning, deduplication, Excel spreadsheet compilation (with Data Quality and Numeric Summary sheets), distribution/categorical charts, and executive HTML presentation reports.
2. NEVER claim unsupported skills (e.g. mobile apps, web design, machine learning models, video editing).
3. Be direct, respectful, and realistic about turnaround and requirements.
4. Mention that deliverables undergo automated 6-point independent QA verification before handover.
5. Structure clearly: Understanding of Scope, Concrete Deliverable Package, Quality Guarantee, Timeline & Terms."""


def generate_proposal_template(opportunity: dict[str, Any]) -> str:
    """Deterministic, high-converting proposal generator when local LLM is offline."""
    title = opportunity.get("title", "Business Data Analysis")
    desc = opportunity.get("description", "").strip()
    amt = float(opportunity.get("amount_inr", 5000))
    hours = float(opportunity.get("estimated_hours", 3))

    cap = CapabilityMatcher.match(title, desc)
    matched_names = [c.replace("_", " ").title() for c in cap.matched_capabilities] or ["CSV & Excel Data Analysis"]

    proposal_text = f"""Hello,

I reviewed your requirements for "{title}". I specialize in structured business data analysis, automated data sanitization, and professional executive reporting.

### Scope & Understanding
{desc if desc else f"Perform thorough data cleaning, metric profiling, and executive reporting for your {title} dataset."}

### Verified Deliverable Package
I will deliver a complete, presentation-ready analytics bundle:
1. **Cleaned & Sanitized Data (`cleaned_data.csv`)**: Deduplicated records with protection against formula injection (=, +, -, @).
2. **Interactive Excel Workbook (`business_analysis.xlsx`)**: Structured multi-sheet workbook including:
   - *Cleaned Data*: Filterable data table with preserved formatting.
   - *Data Quality Audit*: Column null rates, unique values, and missing cell tallies.
   - *Numeric Summary*: Comprehensive statistical breakdown (means, min/max, percentiles).
3. **Visual Analytics (`charts/`)**: High-resolution distribution histograms and categorical ranking bar charts.
4. **Executive Presentation (`report.html`)**: Standalone responsive HTML report with KPI highlight cards and analyst observations.
5. **Machine-Readable Metadata (`profile.json`)**: Full programmatic dataset profiling.

### Independent Quality Assurance
Every file and calculation undergoes our automated 6-point Quality Assurance audit (verifying workbook integrity, mathematical consistency, chart headers, and file health) before final handover.

### Commercial Terms & Delivery
- **Timeline**: Delivered within {max(24, int(hours * 8))} hours upon receiving the dataset.
- **Budget**: ₹{amt:,.2f} fixed milestone.

I am ready to review your sample dataset and begin immediately.

Best regards,
Bhavesh Khullar · SOLOEARN Analytics
"""
    return proposal_text.strip()


def draft_tailored_proposal(
    opportunity_id: int,
    llm_call: Callable[[str, str], str] | None = None,
) -> tuple[int, str, str]:
    """
    Generates a personalized proposal for the given opportunity ID.
    Uses local Ollama if provided and responsive; otherwise uses verified template generator.
    Saves draft into SQLite proposals table and returns (proposal_id, proposal_text, model_name).
    """
    opp = get_opportunity(opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity #{opportunity_id} not found")

    title = opp.get("title", "")
    desc = opp.get("description", "")
    amt = opp.get("amount_inr", 0.0)
    hours = opp.get("estimated_hours", 1.0)

    proposal_text = ""
    model_used = "deterministic_template"

    if llm_call:
        prompt = f"""Generate a professional client proposal for this freelance job:
Title: {title}
Description: {desc}
Offered Budget: ₹{amt:,.2f}
Estimated Hours: {hours}

Follow the required structure: Understanding of Scope, Concrete Deliverable Package (Cleaned CSV, Excel with Data Quality and Numeric Summary, Charts, and HTML report), 6-point QA Guarantee, and Timeline/Terms."""
        try:
            res = llm_call(PROPOSAL_SYSTEM_PROMPT, prompt)
            if res and len(res.strip()) > 150:
                proposal_text = res.strip()
                model_used = "local_qwen3_4b"
        except Exception:
            proposal_text = ""

    if not proposal_text:
        proposal_text = generate_proposal_template(opp)
        model_used = "deterministic_template"

    proposal_id = save_proposal_draft(opportunity_id, proposal_text, model_used)
    log_event("proposal_generated", f"opp_id={opportunity_id}; model={model_used}; length={len(proposal_text)}")
    return proposal_id, proposal_text, model_used


def authorize_proposal(proposal_id: int) -> None:
    """Explicit human authorization gate: marks proposal approved by owner."""
    update_proposal_status(proposal_id, "approved_by_owner")
    log_event("proposal_authorized_by_owner", f"proposal_id={proposal_id}")
