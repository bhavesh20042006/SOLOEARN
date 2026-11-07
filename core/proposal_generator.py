from __future__ import annotations

from typing import Any, Callable

from core.capability_matcher import CapabilityMatcher
from core.db import get_opportunity, log_event, save_proposal_draft, update_proposal_status


# Client-facing proposals are deliberately deterministic until claims can be checked.

def generate_proposal_template(opportunity: dict[str, Any]) -> str:
    """Create an honest proposal draft without inventing scope, price, or delivery dates."""
    title = str(opportunity.get("title") or "Business Data Analysis").strip()[:180]
    desc = str(opportunity.get("description") or "").strip()[:4000]
    try:
        amt = float(opportunity.get("amount_inr") or 0.0)
    except (TypeError, ValueError):
        amt = 0.0
    if not (amt > 0 and amt < float("inf")):
        amt = 0.0

    cap = CapabilityMatcher.match(title, desc)
    if cap.unsupported_requirements:
        raise ValueError("This opportunity includes unsupported requirements; do not draft a proposal that implies SOLOEARN can deliver them.")

    budget_text = (
        f"The source lists an indicative budget of ₹{amt:,.2f}. This is not an accepted quote; final fees require scope and data review."
        if amt > 0
        else "The source does not state a verifiable budget. No price is assumed or quoted in this draft; fees must be agreed after scope and data review."
    )
    scope_text = desc if desc else "The source contains limited scope information. A detailed scope must be confirmed before work is accepted."

    proposal_text = f"""Hello,

I reviewed the opportunity: "{title}". My current verified delivery workflow focuses on structured tabular-data work: CSV cleaning, deduplication, Excel compilation, statistical summaries, charts, and HTML reporting.

### Scope & Understanding
{scope_text}

### Capabilities relevant to this request
{', '.join(name.replace('_', ' ').title() for name in cap.matched_capabilities) if cap.matched_capabilities else 'Potentially suitable for data analysis; the exact scope and input format must be clarified before confirming capability.'}

### Potential deliverables (subject to confirming the input data and final scope)
1. Cleaned and sanitized CSV (`cleaned_data.csv`), where appropriate.
2. Excel workbook (`business_analysis.xlsx`) with data-quality and numeric-summary sheets.
3. Relevant charts and a standalone HTML report (`report.html`).
4. A deliverables package with a QA report.

### 6-point Quality Assurance
Before handover, the current workflow runs six automated QA checks covering deliverable presence, workbook integrity, calculation consistency, chart validity, HTML report integrity, and archive integrity. This does not replace client acceptance or a review of project-specific requirements.

### Commercial terms — not yet agreed
{budget_text}
Delivery timing will be confirmed only after reviewing a representative dataset, row count, data condition, dependencies, and the final scope. This draft is not an acceptance of the job or a binding commitment.

I would be happy to review a representative, non-sensitive sample and confirm whether the full scope fits the verified workflow.

Best regards,
SOLOEARN Analytics
"""
    return proposal_text.strip()

def draft_tailored_proposal(
    opportunity_id: int,
    llm_call: Callable[[str, str], str] | None = None,
) -> tuple[int, str, str]:
    """
    Generates a deterministic, reviewable client proposal for the given opportunity ID.
    llm_call is retained for API compatibility but is intentionally not used for external drafts
    until generated claims can be checked against a strict structured fact policy.
    Saves draft into SQLite proposals table and returns (proposal_id, proposal_text, model_name).
    """
    opp = get_opportunity(opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity #{opportunity_id} not found")

    # Client-facing proposals stay deterministic until generated text can be
    # validated against a structured fact/commitment policy. A local LLM may
    # assist internally, but its unverified claims are not sent into the draft.
    proposal_text = generate_proposal_template(opp)
    model_used = "deterministic_template"

    proposal_id = save_proposal_draft(opportunity_id, proposal_text, model_used)
    log_event("proposal_generated", f"opp_id={opportunity_id}; model={model_used}; length={len(proposal_text)}")
    return proposal_id, proposal_text, model_used


def authorize_proposal(proposal_id: int) -> None:
    """Explicit human authorization gate: marks proposal approved by owner."""
    update_proposal_status(proposal_id, "approved_by_owner")
    log_event("proposal_authorized_by_owner", f"proposal_id={proposal_id}")
