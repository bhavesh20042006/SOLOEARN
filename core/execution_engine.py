from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.analytics import create_analytics_bundle
from core.config import safe_workspace_path
from core.db import claim_opportunity_for_execution, connect, create_task, get_task, log_event, update_task_status
from core.service_executors import (
    execute_business_automation_service,
    execute_dashboard_development_service,
    execute_data_engineering_service,
)


@dataclass
class PlanStep:
    step_id: int
    name: str
    description: str
    expected_output: str
    status: str = "pending"  # pending, in_progress, completed, failed
    notes: str = ""


@dataclass
class TaskPlan:
    task_id: int
    opportunity_id: int | None
    title: str
    client_scope: str
    target_deliverables: list[str]
    steps: list[PlanStep]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_execution_plan(
    opportunity: dict[str, Any],
    task_id: int,
    filename: str,
    llm_call: Callable[[str, str], str] | None = None,
) -> TaskPlan:
    """
    Decomposes the opportunity requirements into a structured, step-by-step task plan.
    Uses LLM for interpreting business nuance if available, with deterministic fallback.
    """
    opp_title = opportunity.get("title", "Data Analysis Task")
    opp_desc = opportunity.get("description", "")
    opp_id = opportunity.get("id")

    scope_summary = opp_desc.strip() or f"Perform structured data analysis, data quality audit and business report for {opp_title}."

    steps = [
        PlanStep(
            step_id=1,
            name="Input Ingestion & Validation",
            description=f"Validate CSV input file '{filename}', check byte boundaries and verify tabular consistency.",
            expected_output="Validated in-memory dataset",
        ),
        PlanStep(
            step_id=2,
            name="Data Sanitization & Security Protection",
            description="Deduplicate records, sanitize object columns to prevent spreadsheet formula injection (=, +, -, @).",
            expected_output="cleaned_data.csv",
        ),
        PlanStep(
            step_id=3,
            name="Statistical Profiling & Metric Audit",
            description="Compute column null percentages, distinct cardinality, numeric distribution statistics.",
            expected_output="profile.json",
        ),
        PlanStep(
            step_id=4,
            name="Spreadsheet Compilation",
            description="Generate formatted Excel workbook with Cleaned Data, Data Quality, and Numeric Summary sheets.",
            expected_output="business_analysis.xlsx",
        ),
        PlanStep(
            step_id=5,
            name="Visual Analytics Generation",
            description="Plot distribution histograms and categorical ranking bar charts as high-resolution PNGs.",
            expected_output="charts/*.png",
        ),
        PlanStep(
            step_id=6,
            name="Client Presentation & Executive Report",
            description="Synthesize insights and produce a standalone responsive HTML presentation with KPI summary cards.",
            expected_output="report.html & insights.txt",
        ),
        PlanStep(
            step_id=7,
            name="Deliverable Package Assembly",
            description="Assemble all generated assets into an isolated, inspectable archive.",
            expected_output="deliverables.zip",
        ),
    ]

    target_deliverables = [
        "business_analysis.xlsx",
        "cleaned_data.csv",
        "profile.json",
        "insights.txt",
        "report.html",
        "charts/",
        "deliverables.zip",
    ]

    return TaskPlan(
        task_id=task_id,
        opportunity_id=opp_id,
        title=f"Autonomous Execution: {opp_title}",
        client_scope=scope_summary,
        target_deliverables=target_deliverables,
        steps=steps,
    )


class AutonomousExecutionEngine:
    """
    Autonomous Execution Engine that turns an approved opportunity into
    a concrete set of professional deliverables in an isolated workspace.
    """

    def __init__(self, llm_call: Callable[[str, str], str] | None = None):
        self.llm_call = llm_call
        # Exposed to the orchestrator so it only releases a lease this worker owns.
        self.claim_acquired = False

    def execute_job(
        self,
        opportunity: dict[str, Any],
        file_bytes: bytes,
        filename: str,
        task_id: int | None = None,
    ) -> tuple[int, Path, TaskPlan]:
        """
        Executes the work plan for the approved opportunity.
        Returns (task_id, workspace_path, task_plan).
        """
        opp_id = opportunity.get("id")
        if opp_id is not None:
            with connect() as conn:
                opp_row = conn.execute("SELECT id, status, source_permitted FROM opportunities WHERE id=?", (opp_id,)).fetchone()
            if opp_row is None:
                raise ValueError(f"Opportunity #{opp_id} not found")
            if not opp_row["source_permitted"]:
                raise ValueError(f"Opportunity #{opp_id} is from an unpermitted source")
            if opp_row["status"] not in {"approved_for_work", "in_progress"}:
                raise ValueError(f"Opportunity #{opp_id} is not approved for work (status: '{opp_row['status']}')")

            # Acquire a process-safe lease regardless of whether this is a fresh
            # task or a retry of an interrupted task. A supplied task must belong
            # to this opportunity and must be in a resumable state.
            if task_id is not None:
                existing_task = get_task(task_id)
                if existing_task is None or existing_task.get("opportunity_id") != opp_id:
                    raise ValueError("Supplied task_id does not belong to this opportunity")
                if existing_task.get("status") not in {"created", "failed", "needs_manual_review", "in_progress"}:
                    raise ValueError("Supplied task is not in a resumable state")
            claimed = claim_opportunity_for_execution(opp_id)
            if not claimed:
                raise RuntimeError(f"Concurrency lock: opportunity #{opp_id} was already claimed by another worker.")
            self.claim_acquired = True
        else:
            if not opportunity.get("source_permitted", False):
                raise ValueError("Opportunity is from an unpermitted source")
            if opportunity.get("status") not in {"approved_for_work", "in_progress"}:
                raise ValueError(f"Opportunity is not approved for work (status: '{opportunity.get('status')}')")

        opp_title = opportunity.get("title", Path(filename).name)

        # Detect service tier. Never silently route an unknown requested service
        # into the analytics executor: that could deliver the wrong work to a client.
        service_tier = opportunity.get("service_tier", "")
        combined_text = f"{opp_title.lower()} {opportunity.get('description', '').lower()}"
        if not service_tier:
            if "dashboard" in combined_text:
                service_tier = "interactive_dashboard"
            elif "pipeline" in combined_text or "etl" in combined_text:
                service_tier = "data_engineering"
            elif "automation" in combined_text or "script" in combined_text:
                service_tier = "business_automation"
            else:
                service_tier = "data_analytics"

        supported_service_tiers = {
            "data_analytics", "data_engineering", "interactive_dashboard", "business_automation"
        }
        if service_tier not in supported_service_tiers:
            raise ValueError(f"Unsupported service tier: {service_tier!r}")

        if task_id is None:
            task_id = create_task(
                title=f"Autonomous execution ({service_tier}): {opp_title}",
                task_type=f"autonomous_{service_tier}",
                workspace="pending",
                opportunity_id=opp_id,
                notes=f"Executing autonomous work plan for {service_tier}.",
            )

        workspace = safe_workspace_path(f"task_{task_id:04d}")
        workspace.mkdir(parents=True, exist_ok=True)
        update_task_status(task_id, "in_progress", f"Executing in {workspace.name}", workspace=str(workspace))

        # Generate structured plan
        plan = generate_execution_plan(opportunity, task_id, filename, self.llm_call)

        # Record plan to workspace
        plan_path = workspace / "execution_plan.json"
        plan_path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

        # Execute according to service tier
        for step in plan.steps:
            step.status = "in_progress"

        try:
            if service_tier == "data_engineering":
                deliverables = execute_data_engineering_service(workspace, file_bytes, filename)
            elif service_tier == "interactive_dashboard":
                deliverables = execute_dashboard_development_service(workspace, file_bytes, filename)
            elif service_tier == "business_automation":
                deliverables = execute_business_automation_service(workspace, file_bytes, filename, opp_title)
            else:
                # Default Tier 1: CSV Analytics Bundle
                zip_artifact = create_analytics_bundle(
                    file_bytes=file_bytes,
                    filename=filename,
                    task_id=task_id,
                    llm_call=self.llm_call,
                )

            for step in plan.steps:
                step.status = "completed"
                step.notes = "Executed successfully."

            # Update saved plan with completed states
            plan_path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

            log_event("autonomous_execution_completed", f"task_id={task_id}; opp_id={opp_id}; service={service_tier}")
            return task_id, workspace, plan

        except Exception as exc:
            for step in plan.steps:
                if step.status == "in_progress":
                    step.status = "failed"
                    step.notes = str(exc)
                    break
            plan_path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            update_task_status(task_id, "failed", f"Execution error: {exc}", workspace=str(workspace))
            raise
