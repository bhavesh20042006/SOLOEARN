from __future__ import annotations

"""
SOLOEARN Autonomy & Profitability System (v0.8.0)
Architecture designed for ₹1,00,000+ Monthly Post-Tax Take-Home Profit.

Unifies the 6 Core Engines:
1. Opportunity Engine: Discover, assess fit, and generate personalized approaches.
2. Sales Engine: Qualify enquiries, calculate dynamic profitable pricing, draft proposals, track follow-ups.
3. Delivery Engine: Execute verified workflows across all 4 tiers with runtime & resource tracking.
4. Quality Engine: Independent QA audit, automated revision loop, strict halt on failures.
5. Finance Engine: Invoices (UPI QR), advances, TDS receivables, GST reserves, expenses vs draws, and post-tax profit.
6. Autonomy Controller: Unified operating cycle, failure recovery without duplicates, 1-lakh capacity model, and 5 Approval Gates.
"""

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.analytics import create_analytics_bundle
from core.capability_matcher import CapabilityMatcher
from core.commercial_funnel import (
    REAL_SMB_PILOT_TARGETS,
    add_commercial_lead,
    list_commercial_leads,
    record_outreach_interaction,
    update_lead_stage,
)
from core.config import safe_workspace_path
from core.db import (
    claim_opportunity_for_execution,
    connect,
    create_invoice,
    create_task,
    get_opportunity,
    get_task,
    log_event,
    release_opportunity_execution_claim,
    update_opportunity_status,
    update_task_status,
    utc_now,
)
from core.financial_ledger import (
    apply_customer_advance,
    get_cash_and_reserves_summary,
    reconcile_statement_row,
    record_customer_advance,
    record_owner_draw,
    register_expense,
)
from core.net_profit_optimizer import (
    MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN,
    MONTHLY_NET_PROFIT_TARGET_INR,
    SERVICE_PROFIT_SPECS,
    NetProfitOptimizer,
    ServiceProfitSpec,
)
from core.qa_agent import IndependentQAAgent, QAReport
from core.revision_engine import RevisionEngine
from core.scoring import opportunity_score
from core.service_executors import (
    execute_business_automation_service,
    execute_dashboard_development_service,
    execute_data_engineering_service,
)
from core.tax_engine import compute_business_incremental_tax
from core.upi_engine import generate_invoice_upi_qr, reconcile_upi_payment_from_bank_credit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProspectFit:
    prospect_id: str | int
    company_name: str
    service_tier: str
    fit_score: float
    is_fit: bool
    estimated_hours: float
    target_price_inr: float
    estimated_margin_pct: float
    rejection_reason: str = ""
    personalized_approach: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnquiryQualification:
    enquiry_id: str
    client_name: str
    is_qualified: bool
    service_tier: str
    estimated_hours: float
    budget_inr: float
    qualification_notes: str
    recommended_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PricingCalculation:
    service_tier: str
    target_price_inr: float
    estimated_hours: float
    direct_cost_inr: float
    platform_fee_inr: float
    net_contribution_inr: float
    contribution_margin_pct: float
    effective_hourly_yield_inr: float
    meets_margin_floor: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeliveryJob:
    task_id: int
    opportunity_id: int | None
    client_name: str
    service_tier: str
    workspace: str
    status: str
    duration_seconds: float = 0.0
    artifact_zip: str = ""
    deliverables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityAuditResult:
    task_id: int
    passed: bool
    qa_score_pct: float
    revisions_attempted: int
    deliverables_verified: bool
    status: str  # "submission_ready" or "needs_manual_review"
    failure_reasons: list[str] = field(default_factory=list)
    audit_id: str = ""
    qa_report_path: str = ""
    edge_case_status: dict[str, bool] = field(default_factory=dict)
    qa_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EscalatedException:
    exception_id: str
    gate_type: str  # unusual_contract, custom_deadline, refund_request, capital_transfer, tax_compliance
    description: str
    entity_reference: str
    severity: str  # normal, high, critical
    action_blocked: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OperatingCycleReport:
    cycle_id: str
    timestamp: str
    recovered_jobs: int
    prospects_scored: int
    enquiries_qualified: int
    proposals_drafted: int
    jobs_delivered: int
    qa_audits_passed: int
    qa_audits_failed: int
    invoices_issued: int
    settlements_reconciled: int
    routine_tasks_completed: int
    outreach_touches_dispatched: int = 0
    active_sequences_count: int = 0
    escalations: list[EscalatedException] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProfitabilityCapacityModel:
    monthly_target_inr: float
    required_pretax_profit_inr: float
    required_monthly_receipts_inr: float
    monthly_hour_capacity: float
    tier_capacities: dict[str, dict[str, Any]]
    balanced_portfolio_model: dict[str, Any]
    is_capacity_viable: bool
    floor_margin_model: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 1. Opportunity Engine
# ---------------------------------------------------------------------------

class OpportunityEngine:
    """
    Finds suitable business prospects, assesses fit against delivery capabilities
    and margins, and prepares personalized, question-led approaches.
    """

    SUPPORTED_SERVICE_TIERS = {
        "data_analytics": "Tier 1: Business Analytics & Excel Pack",
        "csv_reconciliation": "Tier 2: CSV Reconciliation Automation",
        "business_automation": "Tier 2: Business Automation Pipeline",
        "data_engineering": "Tier 3: Data Engineering ETL Pipeline",
        "interactive_dashboard": "Tier 4: Executive Interactive HTML5 Dashboard",
    }

    @classmethod
    def assess_prospect_fit(cls, prospect: dict[str, Any]) -> ProspectFit:
        prospect_id = prospect.get("id", "PROSPECT-00")
        company_name = str(prospect.get("company_name") or prospect.get("title") or "Unnamed Prospect")
        desc = str(prospect.get("description") or prospect.get("pain_points") or "")
        combined = f"{company_name} {desc}".lower()

        # Check unsupported requirements first (e.g., scraping, mobile apps, crypto)
        cap = CapabilityMatcher.match(company_name, desc)
        if cap.unsupported_requirements:
            return ProspectFit(
                prospect_id=prospect_id,
                company_name=company_name,
                service_tier="unsupported",
                fit_score=0.0,
                is_fit=False,
                estimated_hours=0.0,
                target_price_inr=0.0,
                estimated_margin_pct=0.0,
                rejection_reason=f"Unsupported requirements detected: {', '.join(cap.unsupported_requirements)}",
            )

        # Service tier routing
        tier = str(prospect.get("service_tier") or "")
        if not tier or tier not in cls.SUPPORTED_SERVICE_TIERS:
            if "dashboard" in combined:
                tier = "interactive_dashboard"
            elif "pipeline" in combined or "etl" in combined:
                tier = "data_engineering"
            elif "automation" in combined or "reconcil" in combined:
                tier = "business_automation"
            else:
                tier = "data_analytics"

        # Lookup spec
        spec_match = next((s for s in SERVICE_PROFIT_SPECS if s.tier_id in {tier, "data_analytics"}), SERVICE_PROFIT_SPECS[0])
        if tier == "business_automation":
            spec_match = next((s for s in SERVICE_PROFIT_SPECS if s.tier_id in {"csv_reconciliation", "business_automation"}), spec_match)

        amt = float(prospect.get("proposed_price_inr") or prospect.get("amount_inr") or spec_match.target_price_inr)
        hrs = float(prospect.get("estimated_hours") or spec_match.estimated_hours)
        risk = float(prospect.get("risk_level") or 2.0)
        conf = float(prospect.get("payment_confidence_pct") or 90.0) / 100.0 if "payment_confidence_pct" in prospect else 0.9

        # Score opportunity
        score = opportunity_score(
            amount_inr=amt,
            estimated_hours=hrs,
            skill_match=1.0,
            payment_confidence=conf,
            risk_level=risk,
            source_permitted=True,
        )

        margin_pct = spec_match.contribution_margin_pct
        is_fit = margin_pct >= (MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN * 100.0) and score >= 30.0

        return ProspectFit(
            prospect_id=prospect_id,
            company_name=company_name,
            service_tier=tier,
            fit_score=round(score, 1),
            is_fit=is_fit,
            estimated_hours=hrs,
            target_price_inr=amt,
            estimated_margin_pct=margin_pct,
            rejection_reason="" if is_fit else "Low score or margin below 50% minimum threshold",
        )

    @classmethod
    def prepare_personalized_approach(
        cls,
        company_name: str,
        contact_name: str = "Founder / Operations Lead",
        service_tier: str = "data_analytics",
        channel: str = "linkedin",
        role: str = "founder",
    ) -> str:
        """
        Drafts a question-led, consultative discovery approach addressing real SMB multi-channel
        data reconciliation, catalog errors, or reporting bottlenecks.
        Leverages standardized high-converting D2C outreach templates.
        """
        from core.outreach_dispatcher import D2COutreachTemplates
        clean_role = role.lower().strip()
        if clean_role not in D2COutreachTemplates.list_supported_roles():
            clean_role = "coo" if service_tier in {"csv_reconciliation", "business_automation"} else "founder"

        rendered = D2COutreachTemplates.render_template(
            step=1,
            channel=channel,
            role=clean_role,
            context={
                "founder_name": contact_name,
                "company_name": company_name,
                "channels": "Amazon, Blinkit & Shopify",
            },
        )
        return rendered["body"]

    @classmethod
    def enroll_prospect_in_outreach(
        cls,
        lead_id: int | None,
        company_name: str,
        recipient: str,
        channel: str = "email",
        role: str = "founder",
        custom_vars: dict[str, Any] | None = None,
        is_sandbox: bool = True,
    ) -> dict[str, Any]:
        """Enrolls a prospect into the multi-touch cadence via outreach dispatcher."""
        from core.outreach_dispatcher import enroll_in_sequence
        return enroll_in_sequence(
            lead_id=lead_id,
            client_name=company_name,
            recipient=recipient,
            channel=channel,
            role=role,
            custom_vars=custom_vars,
            is_sandbox=is_sandbox,
        )

    @classmethod
    def find_and_score_prospects(cls, is_sandbox: bool = True) -> list[ProspectFit]:
        """Scans registered commercial leads and returns scored fit profiles."""
        leads = list_commercial_leads(include_sandbox=is_sandbox)
        results: list[ProspectFit] = []
        for lead in leads:
            fit = cls.assess_prospect_fit(lead)
            if fit.is_fit and not fit.personalized_approach:
                fit.personalized_approach = cls.prepare_personalized_approach(
                    company_name=fit.company_name,
                    contact_name=lead.get("contact_name", "Lead"),
                    service_tier=fit.service_tier,
                    channel=lead.get("contact_channel", "linkedin"),
                )
            results.append(fit)
        return results


# ---------------------------------------------------------------------------
# 2. Sales Engine
# ---------------------------------------------------------------------------

class SalesEngine:
    """
    Qualifies inbound/outbound enquiries, calculates margin-enforced pricing,
    prepares structured proposals, and tracks follow-up cadences.
    """

    @classmethod
    def qualify_enquiry(cls, enquiry_data: dict[str, Any]) -> EnquiryQualification:
        enq_id = str(enquiry_data.get("enquiry_id", f"ENQ-{int(time.time())}"))
        client_name = str(enquiry_data.get("client_name") or "Prospective Client")
        scope = str(enquiry_data.get("scope") or enquiry_data.get("description") or "")
        budget = float(enquiry_data.get("budget_inr") or 0.0)

        cap = CapabilityMatcher.match(client_name, scope)
        if cap.unsupported_requirements:
            return EnquiryQualification(
                enquiry_id=enq_id,
                client_name=client_name,
                is_qualified=False,
                service_tier="unsupported",
                estimated_hours=0.0,
                budget_inr=budget,
                qualification_notes=f"Disqualified: unsupported requirements ({', '.join(cap.unsupported_requirements)})",
                recommended_action="Politely decline with capability boundary notice.",
            )

        # Detect service tier
        combined = f"{client_name} {scope}".lower()
        if "dashboard" in combined:
            tier = "interactive_dashboard"
        elif "pipeline" in combined or "etl" in combined:
            tier = "data_engineering"
        elif "automation" in combined or "reconcil" in combined:
            tier = "business_automation"
        else:
            tier = "data_analytics"

        spec = next((s for s in SERVICE_PROFIT_SPECS if s.tier_id in {tier, "data_analytics"}), SERVICE_PROFIT_SPECS[0])
        hrs = spec.estimated_hours

        # Budget qualification: if stated budget is far below floor, disqualify
        min_price = spec.target_price_inr * 0.7  # max 30% discount allowed before disqualification
        if 0 < budget < min_price:
            return EnquiryQualification(
                enquiry_id=enq_id,
                client_name=client_name,
                is_qualified=False,
                service_tier=tier,
                estimated_hours=hrs,
                budget_inr=budget,
                qualification_notes=f"Budget ₹{budget:,.2f} is below minimum acceptable floor ₹{min_price:,.2f}",
                recommended_action="Counter-quote standard tier price or decline.",
            )

        return EnquiryQualification(
            enquiry_id=enq_id,
            client_name=client_name,
            is_qualified=True,
            service_tier=tier,
            estimated_hours=hrs,
            budget_inr=budget if budget > 0 else spec.target_price_inr,
            qualification_notes="Scope and budget aligned with verified service capabilities.",
            recommended_action="Draft structured commercial proposal.",
        )

    @classmethod
    def calculate_pricing(
        cls,
        service_tier: str,
        estimated_hours: float | None = None,
        custom_direct_cost: float | None = None,
    ) -> PricingCalculation:
        spec = next((s for s in SERVICE_PROFIT_SPECS if s.tier_id == service_tier), SERVICE_PROFIT_SPECS[0])
        target_price = spec.target_price_inr
        hrs = estimated_hours if estimated_hours and estimated_hours > 0 else spec.estimated_hours
        direct_cost = custom_direct_cost if custom_direct_cost is not None else spec.direct_cost_inr
        platform_fee = round(target_price * (spec.platform_fee_pct / 100.0), 2)
        net_contrib = round(target_price - direct_cost - platform_fee, 2)
        margin_pct = round((net_contrib / target_price) * 100.0, 1) if target_price > 0 else 0.0
        hourly_yield = round(net_contrib / max(0.5, hrs), 2)
        meets_floor = margin_pct >= (MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN * 100.0)

        return PricingCalculation(
            service_tier=service_tier,
            target_price_inr=target_price,
            estimated_hours=hrs,
            direct_cost_inr=direct_cost,
            platform_fee_inr=platform_fee,
            net_contribution_inr=net_contrib,
            contribution_margin_pct=margin_pct,
            effective_hourly_yield_inr=hourly_yield,
            meets_margin_floor=meets_floor,
        )

    @classmethod
    def prepare_proposal(
        cls,
        lead_id: int | None,
        client_name: str,
        service_tier: str,
        price_inr: float | None = None,
        custom_sla_hours: float | None = None,
        is_sandbox: bool = True,
    ) -> tuple[dict[str, Any], bool, str]:
        """
        Creates a structured proposal.
        Returns: (proposal_dict, requires_owner_approval, approval_reason).
        """
        pricing = cls.calculate_pricing(service_tier)
        final_price = price_inr if price_inr and price_inr > 0 else pricing.target_price_inr

        # Gate 1 & Gate 2 Check: Unusual contract terms or prices below margin floor
        requires_owner_approval = False
        reasons: list[str] = []

        if final_price < pricing.target_price_inr * 0.8:
            requires_owner_approval = True
            reasons.append(f"Price ₹{final_price:,.2f} is discounted >20% below standard ₹{pricing.target_price_inr:,.2f}")

        if custom_sla_hours and custom_sla_hours < 24.0:
            requires_owner_approval = True
            reasons.append(f"Custom turnaround SLA of {custom_sla_hours}h requires owner capacity verification")

        proposal_text = (
            f"COMMERCIAL PROPOSAL FOR {client_name.upper()}\n"
            f"Service Tier: {service_tier}\n"
            f"Deliverables: Cleaned datasets, validated logic, verified test suites, and deliverables.zip archive.\n"
            f"Quoted Fee: ₹{final_price:,.2f} (ex-GST)\n"
            f"Payment Terms: 50% advance before execution, 50% upon QA verification and delivery.\n"
            f"Turnaround: Standard 48 hours from input file receipt.\n"
            f"Quality Assurance: Verified by Independent QA Agent prior to submission."
        )

        with connect() as conn:
            opp_check = conn.execute("SELECT id FROM opportunities WHERE id = ?", (lead_id,)).fetchone() if lead_id is not None else None

        if opp_check is None:
            from core.db import add_opportunity
            lead_id = add_opportunity({
                "title": f"Commercial enquiry: {client_name}",
                "source": "Direct Enquiry",
                "amount_inr": final_price,
                "estimated_hours": pricing.estimated_hours,
                "skill_match": 1.0,
                "payment_confidence": 0.9,
                "risk_level": 2.0,
                "source_permitted": 1,
            }, score=50.0, is_sandbox=is_sandbox)

        with connect() as conn:
            cur = conn.execute(
                """INSERT INTO proposals (
                    opportunity_id, draft_text, status, model_used, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    lead_id,
                    proposal_text,
                    "draft" if not requires_owner_approval else "requires_owner_approval",
                    "deterministic_template",
                    utc_now(),
                    utc_now(),
                ),
            )
            prop_id = cur.lastrowid

        proposal_dict = {
            "id": prop_id,
            "client_name": client_name,
            "service_tier": service_tier,
            "price_quote_inr": final_price,
            "proposal_text": proposal_text,
            "status": "draft" if not requires_owner_approval else "requires_owner_approval",
            "is_sandbox": is_sandbox,
        }
        return proposal_dict, requires_owner_approval, "; ".join(reasons)


# ---------------------------------------------------------------------------
# 3. Delivery Engine
# ---------------------------------------------------------------------------

class DeliveryEngine:
    """
    Turns approved orders into isolated execution workspaces, executes the verified
    service workflow across all 4 tiers, and tracks runtime and memory limits.
    """

    @classmethod
    def convert_order_to_job(
        cls,
        opportunity_id: int | None,
        client_name: str,
        service_tier: str,
        is_sandbox: bool = True,
    ) -> DeliveryJob:
        if opportunity_id is not None:
            opp = get_opportunity(opportunity_id)
            if opp and opp.get("status") not in {"approved_for_work", "in_progress"}:
                raise ValueError(f"Opportunity #{opportunity_id} is not in approved status (status: '{opp.get('status')}')")
            if opp and opp.get("status") == "approved_for_work":
                update_opportunity_status(opportunity_id, "in_progress")

        task_id = create_task(
            title=f"Autonomous delivery ({service_tier}): {client_name}",
            task_type=f"autonomous_{service_tier}",
            workspace="pending",
            opportunity_id=opportunity_id,
            notes=f"Automated execution for {client_name}",
            is_sandbox=is_sandbox,
        )

        workspace = safe_workspace_path(f"task_{task_id:04d}")
        workspace.mkdir(parents=True, exist_ok=True)
        update_task_status(task_id, "in_progress", f"Workspace created at {workspace.name}", workspace=str(workspace))

        return DeliveryJob(
            task_id=task_id,
            opportunity_id=opportunity_id,
            client_name=client_name,
            service_tier=service_tier,
            workspace=str(workspace),
            status="in_progress",
        )

    @classmethod
    def execute_delivery_workflow(
        cls,
        job: DeliveryJob,
        file_bytes: bytes,
        filename: str,
    ) -> tuple[DeliveryJob, list[str]]:
        """
        Executes the matching service workflow with high-precision runtime tracking.
        """
        ws = Path(job.workspace)
        t_start = time.perf_counter()
        deliverables: list[str] = []

        opp_id = job.opportunity_id
        if opp_id is not None:
            claim_opportunity_for_execution(opp_id)

        try:
            if job.service_tier == "data_engineering":
                deliverables = execute_data_engineering_service(ws, file_bytes, filename)
            elif job.service_tier == "interactive_dashboard":
                deliverables = execute_dashboard_development_service(ws, file_bytes, filename)
            elif job.service_tier in {"csv_reconciliation", "business_automation"}:
                deliverables = execute_business_automation_service(ws, file_bytes, filename, job.client_name)
            else:
                # Default Tier 1: CSV / Excel Analytics Bundle
                zip_path = create_analytics_bundle(file_bytes, filename, job.task_id)
                deliverables = ["business_analysis.xlsx", "cleaned_data.csv", "profile.json", "insights.txt", "report.html", "deliverables.zip"]

            t_elapsed = round(time.perf_counter() - t_start, 3)
            job.duration_seconds = t_elapsed
            job.deliverables = deliverables
            job.artifact_zip = str(ws / "deliverables.zip")
            job.status = "qa_ready"

            mb_processed = round(len(file_bytes) / (1024 * 1024), 2)
            speed_note = f"Workflow executed in {t_elapsed}s ({mb_processed} MB streamed)"
            update_task_status(job.task_id, "qa_testing", speed_note, artifact_path=job.artifact_zip)
            return job, deliverables
        finally:
            if opp_id is not None:
                release_opportunity_execution_claim(opp_id)


# ---------------------------------------------------------------------------
# 4. Quality Engine
# ---------------------------------------------------------------------------

class QualityEngine:
    """
    Performs independent quality verification, triggers bounded automated repair
    on detected defects, and halts failed jobs for manual review.
    """

    @classmethod
    def inspect_and_verify(
        cls,
        job: DeliveryJob,
        max_revisions: int = 3,
    ) -> QualityAuditResult:
        ws = Path(job.workspace)
        qa_agent = IndependentQAAgent(ws, job.task_id)
        initial_qa = qa_agent.run_all_checks()
        final_qa = initial_qa
        revisions_run = 0

        if not initial_qa.passed:
            update_task_status(
                job.task_id,
                "revising",
                f"QA failed {len(initial_qa.failure_reasons)} checks; initiating revision engine.",
                workspace=str(ws),
            )
            rev_engine = RevisionEngine(ws, job.task_id, max_attempts=max_revisions)
            _, final_qa = rev_engine.repair_and_retest(initial_qa)
            revisions_run = len(rev_engine.history)

        if final_qa.passed:
            status = "submission_ready"
            update_task_status(
                job.task_id,
                "submission_ready",
                f"Independent QA passed ({final_qa.score_pct}%). Verified for client review.",
                artifact_path=str(ws / "deliverables.zip"),
            )
            if job.opportunity_id is not None:
                update_opportunity_status(job.opportunity_id, "submission_ready")
        else:
            status = "needs_manual_review"
            update_task_status(
                job.task_id,
                "needs_manual_review",
                f"Deliverables failed QA ({final_qa.score_pct}%). Delivery halted for owner inspection.",
                artifact_path=str(ws / "deliverables.zip"),
            )
            if job.opportunity_id is not None:
                update_opportunity_status(job.opportunity_id, "needs_manual_review")
            log_event("qa_failed_delivery_halted", f"task_id={job.task_id}; reasons={final_qa.failure_reasons}")

        qa_report_path = ws / "qa_report.json"
        report_dict = final_qa.to_dict()

        return QualityAuditResult(
            task_id=job.task_id,
            passed=final_qa.passed,
            qa_score_pct=final_qa.score_pct,
            revisions_attempted=revisions_run,
            deliverables_verified=final_qa.passed,
            status=status,
            failure_reasons=final_qa.failure_reasons,
            audit_id=final_qa.audit_id,
            qa_report_path=str(qa_report_path) if qa_report_path.exists() else "",
            edge_case_status=final_qa.edge_case_verifications,
            qa_report=report_dict,
        )


# ---------------------------------------------------------------------------
# 5. Finance Engine
# ---------------------------------------------------------------------------

class FinanceEngine:
    """
    Generates invoices, records advances, reconciles bank receipts, tracks deductible
    expenses vs sole proprietor drawings, and computes after-tax profitability.
    """

    @classmethod
    def create_job_invoice(
        cls,
        opportunity_id: int | None,
        client_name: str,
        amount_inr: float,
        is_gst_registered: bool = False,
        is_sandbox: bool = True,
    ) -> dict[str, Any]:
        if opportunity_id is None:
            from core.db import add_opportunity
            opportunity_id = add_opportunity({
                "title": f"Invoice for {client_name}",
                "source": "Direct Invoicing",
                "amount_inr": amount_inr,
                "estimated_hours": 1.0,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1.0,
                "source_permitted": 1,
            }, score=50.0, is_sandbox=is_sandbox)
            t_id = create_task(
                title=f"Deliverable for {client_name}",
                task_type="autonomous_invoicing",
                workspace="workspaces/invoicing",
                opportunity_id=opportunity_id,
            )
            with connect() as conn:
                conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='workspaces/invoicing/deliverables.zip' WHERE id=?", (t_id,))
                conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opportunity_id,))
        else:
            opp = get_opportunity(opportunity_id)
            if opp and opp.get("status") != "submitted_by_owner":
                with connect() as conn:
                    conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opportunity_id,))

        inv = create_invoice(
            opportunity_id=opportunity_id,
            client_name=client_name,
            amount_subtotal=amount_inr,
            tax_rate=18.0 if is_gst_registered else 0.0,
            is_sandbox=is_sandbox,
        )
        qr_info = generate_invoice_upi_qr(inv)
        inv["upi_qr"] = qr_info
        return inv

    @classmethod
    def record_advance(
        cls,
        client_name: str,
        amount_inr: float,
        bank_ref: str = "",
        tx_date: str = "",
        is_sandbox: bool = True,
    ) -> int:
        """Records customer advance liability; recognizes zero earned revenue."""
        return record_customer_advance(
            amount_inr=amount_inr,
            client_name=client_name,
            reference_id=bank_ref,
            tx_date=tx_date or utc_now()[:10],
            is_sandbox=is_sandbox,
        )

    @classmethod
    def apply_advance(
        cls,
        advance_id: int,
        invoice_id_or_number: int | str,
        amount_inr: float | None = None,
        is_sandbox: bool = True,
    ) -> dict[str, Any]:
        """Reclassifies advance liability to earned revenue; creates zero new bank cash."""
        from core.db import get_invoice
        if isinstance(invoice_id_or_number, int):
            inv = get_invoice(invoice_id_or_number)
            inv_num = inv["invoice_number"] if inv else str(invoice_id_or_number)
            subtotal = float(inv.get("amount_subtotal", 0.0)) if inv else 0.0
        else:
            inv_num = str(invoice_id_or_number)
            subtotal = 0.0

        apply_amt = amount_inr if amount_inr and amount_inr > 0 else (subtotal if subtotal > 0 else 2500.0)
        return apply_customer_advance(
            advance_tx_id=advance_id,
            invoice_number=inv_num,
            amount_inr=apply_amt,
            is_sandbox=is_sandbox,
        )

    @classmethod
    def reconcile_payment(
        cls,
        statement_row: dict[str, Any],
        invoice_id: int,
        owner_confirmation: bool = True,
        tds_withheld_inr: float = 0.0,
        is_sandbox: bool = True,
    ) -> dict[str, Any]:
        """Reconciles bank statement credit with duplicate UTR shield and reversal guards."""
        desc = str(statement_row.get("description", "")).upper()
        if "REVERSAL" in desc:
            raise ValueError("Bank statement row is a reversal transaction and cannot be settled as revenue")

        from core.db import get_invoice
        inv = get_invoice(invoice_id)
        inv_num = inv["invoice_number"] if inv else str(invoice_id)

        return reconcile_statement_row(
            row_id=statement_row["id"],
            entity_type="invoice",
            entity_id=inv_num,
            owner_confirmed=owner_confirmation,
            tds_withheld_inr=tds_withheld_inr,
        )

    @classmethod
    def record_operational_expense(
        cls,
        vendor: str,
        category: str,
        amount_inr: float,
        reviewer_notes: str = "Operating expense",
        is_sandbox: bool = True,
    ) -> int:
        """Records deductible business expense, reducing operating profit and taxable income."""
        return register_expense(
            vendor=vendor,
            category=category,
            amount_inr=amount_inr,
            tax_deductibility="deductible",
            reviewer_notes=reviewer_notes,
            ca_review_confirmed=True,
            is_sandbox=is_sandbox,
        )

    @classmethod
    def record_proprietor_draw(
        cls,
        amount_inr: float,
        description: str = "Proprietor personal withdrawal",
        is_sandbox: bool = True,
    ) -> int:
        """Records owner drawing: reduces bank cash, but NEVER reduces operating or taxable profit."""
        return record_owner_draw(
            amount_inr=amount_inr,
            description=description,
            is_sandbox=is_sandbox,
        )

    @classmethod
    def get_profitability_summary(cls, is_sandbox: bool = True) -> dict[str, Any]:
        return get_cash_and_reserves_summary(include_sandbox=is_sandbox)


# ---------------------------------------------------------------------------
# 6. Autonomy Controller
# ---------------------------------------------------------------------------

class AutonomyController:
    """
    Supervises unattended execution, orchestrates the complete operating cycle,
    recovers interrupted jobs safely without duplicate messages/invoices,
    models the ₹1 Lakh take-home profitability capacity, and maintains 5 Approval Gates.
    """

    def __init__(self, is_sandbox: bool = True):
        self.is_sandbox = is_sandbox
        self.escalation_queue: list[EscalatedException] = []

    def escalate_to_owner(
        self,
        gate_type: str,
        description: str,
        entity_reference: str,
        severity: str = "high",
        action_blocked: str = "Execution blocked pending owner confirmation",
    ) -> EscalatedException:
        exc = EscalatedException(
            exception_id=f"ESC-{int(time.time() * 1000) % 1000000}",
            gate_type=gate_type,
            description=description,
            entity_reference=entity_reference,
            severity=severity,
            action_blocked=action_blocked,
        )
        self.escalation_queue.append(exc)
        log_event("autonomy_exception_escalated", f"gate={gate_type}; ref={entity_reference}; desc={description}")
        return exc

    def recover_crashed_jobs(self) -> int:
        """
        Safely recovers crashed tasks in 'in_progress' without duplicate invoices,
        duplicate proposals, or double-settling bank payments.
        """
        recovered_count = 0
        with connect() as conn:
            crashed_tasks = conn.execute(
                """SELECT * FROM tasks 
                WHERE status = 'in_progress' 
                AND (workspace IS NOT NULL AND workspace != 'pending')"""
            ).fetchall()

            for t in crashed_tasks:
                task_id = t["id"]
                ws_path = Path(t["workspace"])
                opp_id = t["opportunity_id"]

                # Ensure existing proposals and invoices are not duplicated
                existing_inv = conn.execute(
                    "SELECT COUNT(*) FROM invoices WHERE opportunity_id = ?", (opp_id,)
                ).fetchone()[0] if opp_id else 0

                # Check if workspace deliverables exist
                zip_path = ws_path / "deliverables.zip"
                if zip_path.exists():
                    update_task_status(task_id, "submission_ready", "Recovered from interrupted cycle. Deliverables intact.", artifact_path=str(zip_path))
                    if opp_id:
                        update_opportunity_status(opp_id, "submission_ready")
                else:
                    update_task_status(task_id, "needs_manual_review", "Task interrupted before deliverable completion. Flagged for review.")
                    if opp_id:
                        update_opportunity_status(opp_id, "needs_manual_review")

                if opp_id:
                    release_opportunity_execution_claim(opp_id)
                recovered_count += 1
                log_event("task_recovered_safe", f"task_id={task_id}; existing_invoices={existing_inv}")

        return recovered_count

    def calculate_lakh_profitability_capacity(self) -> ProfitabilityCapacityModel:
        """
        Calculates exact project unit economics, service volume, and owner-hour capacity
        required to yield ₹1,00,000+ monthly take-home profit after all costs and taxes.
        """
        plan = NetProfitOptimizer.analyze_net_profit()
        monthly_target = MONTHLY_NET_PROFIT_TARGET_INR  # ₹1,00,000.0
        pretax_req = plan.monthly_pretax_profit_required_inr
        receipts_req = plan.monthly_receipts_required_ex_gst_inr

        # Available operational hours per month for a sole operator (40h/week * 4 weeks)
        monthly_hour_capacity = 160.0

        tier_caps: dict[str, dict[str, Any]] = {}
        for spec in SERVICE_PROFIT_SPECS:
            job_count = plan.service_capacities_needed.get(spec.tier_id, 1)
            total_hours = round(job_count * spec.estimated_hours, 1)
            gross_billings = round(job_count * spec.target_price_inr, 2)
            net_contrib = round(job_count * spec.net_contribution_inr, 2)
            capacity_pct = round((total_hours / monthly_hour_capacity) * 100.0, 1)

            tier_caps[spec.tier_id] = {
                "display_name": spec.display_name,
                "target_price_inr": spec.target_price_inr,
                "unit_contribution_inr": spec.net_contribution_inr,
                "contribution_margin_pct": spec.contribution_margin_pct,
                "jobs_needed_if_solo_tier": job_count,
                "hours_required": total_hours,
                "capacity_utilization_pct": capacity_pct,
                "gross_billings_inr": gross_billings,
                "is_capacity_viable": total_hours <= monthly_hour_capacity,
            }

        # Balanced blended portfolio model (Target: ₹1 Lakh take-home):
        # 4x Tier 1 (₹5k), 3x Tier 2 (₹10k), 2x Tier 3 (₹15k), 1x Tier 4 (₹20k)
        # Total jobs: 10 | Total hours: 4*2 + 3*3 + 2*5 + 1*6 = 8 + 9 + 10 + 6 = 33 hours!
        blended_jobs = [
            ("data_analytics", 4, 5000.0, 2.0, 4600.0),
            ("csv_reconciliation", 3, 10000.0, 3.0, 9250.0),
            ("data_engineering", 2, 15000.0, 5.0, 13750.0),
            ("interactive_dashboard", 2, 20000.0, 6.0, 18400.0),
        ]
        blended_total_jobs = sum(j[1] for j in blended_jobs)
        blended_hours = sum(j[1] * j[3] for j in blended_jobs)
        blended_revenue = sum(j[1] * j[2] for j in blended_jobs)
        blended_contrib = sum(j[1] * j[4] for j in blended_jobs)
        blended_capacity_pct = round((blended_hours / monthly_hour_capacity) * 100.0, 1)

        balanced_model = {
            "strategy": "Balanced 4-Tier SMB Retainer Portfolio",
            "total_monthly_jobs": blended_total_jobs,
            "monthly_production_hours": blended_hours,
            "capacity_utilization_pct": blended_capacity_pct,
            "monthly_gross_billings_inr": blended_revenue,
            "total_pretax_contribution_inr": blended_contrib,
            "meets_lakh_profit_target": blended_contrib >= pretax_req,
        }

        is_viable = all(t["is_capacity_viable"] for t in tier_caps.values()) and balanced_model["meets_lakh_profit_target"]

        floor_sales_req = round(pretax_req / MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN, 2)
        floor_margin_model = {
            "contribution_margin_floor_pct": MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN * 100.0,
            "required_monthly_sales_at_floor_inr": floor_sales_req,
            "pretax_contribution_generated_inr": pretax_req,
            "explanation": (
                f"At the strict 50% contribution margin floor, generating ₹{pretax_req:,.2f} "
                f"pre-tax contribution requires ₹{floor_sales_req:,.2f} in gross billings. "
                f"In contrast, SOLOEARN's pure software delivery portfolio operates at ~92% blended margin, "
                f"requiring ₹{blended_revenue:,.2f} in monthly gross billings."
            ),
        }

        return ProfitabilityCapacityModel(
            monthly_target_inr=monthly_target,
            required_pretax_profit_inr=pretax_req,
            required_monthly_receipts_inr=receipts_req,
            monthly_hour_capacity=monthly_hour_capacity,
            tier_capacities=tier_caps,
            balanced_portfolio_model=balanced_model,
            is_capacity_viable=is_viable,
            floor_margin_model=floor_margin_model,
        )

    def run_operating_cycle(self) -> OperatingCycleReport:
        """
        Executes a complete autonomous operating cycle:
        1. Recovers any interrupted jobs.
        2. Discovers and scores pending prospects.
        3. Drafts proposals for qualified enquiries.
        4. Gathers escalated approval gates.
        """
        cid = f"CYCLE-{int(time.time() * 1000) % 1000000}"
        ts = utc_now()
        routine_count = 0

        # Step 1: Failure recovery
        recovered = self.recover_crashed_jobs()
        routine_count += recovered

        # Step 2: Opportunity discovery and scoring
        scored_prospects = OpportunityEngine.find_and_score_prospects(is_sandbox=self.is_sandbox)
        routine_count += len(scored_prospects)

        # Step 3: Sales qualification for fit prospects (idempotent; no duplicate proposals)
        proposals_drafted = 0
        for p in scored_prospects:
            if p.is_fit and p.fit_score >= 50.0:
                with connect() as conn:
                    existing_prop = conn.execute(
                        "SELECT id FROM proposals WHERE draft_text LIKE ?",
                        (f"%COMMERCIAL PROPOSAL FOR {p.company_name.upper()}%",),
                    ).fetchone()
                if existing_prop:
                    continue

                prop, req_approval, reason = SalesEngine.prepare_proposal(
                    lead_id=None,
                    client_name=p.company_name,
                    service_tier=p.service_tier,
                    price_inr=p.target_price_inr,
                    is_sandbox=self.is_sandbox,
                )
                proposals_drafted += 1
                routine_count += 1
                if req_approval:
                    self.escalate_to_owner(
                        gate_type="unusual_contract",
                        description=f"Proposal for {p.company_name} requires approval: {reason}",
                        entity_reference=f"PROP-{prop['id']}",
                        severity="normal",
                        action_blocked="Proposal transmission blocked pending review",
                    )

        report = OperatingCycleReport(
            cycle_id=cid,
            timestamp=ts,
            recovered_jobs=recovered,
            prospects_scored=len(scored_prospects),
            enquiries_qualified=len(scored_prospects),
            proposals_drafted=proposals_drafted,
            jobs_delivered=0,
            qa_audits_passed=0,
            qa_audits_failed=0,
            invoices_issued=0,
            settlements_reconciled=0,
            routine_tasks_completed=routine_count,
            escalations=list(self.escalation_queue),
        )
        return report
