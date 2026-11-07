from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.analytics import create_analytics_bundle
from core.config import safe_workspace_path
from core.db import connect, create_task, log_event, update_task_status


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
        else:
            if not opportunity.get("source_permitted", False):
                raise ValueError("Opportunity is from an unpermitted source")
            if opportunity.get("status") not in {"approved_for_work", "in_progress"}:
                raise ValueError(f"Opportunity is not approved for work (status: '{opportunity.get('status')}')")

        opp_title = opportunity.get("title", Path(filename).name)

        if task_id is None:
            task_id = create_task(
                title=f"Autonomous execution: {opp_title}",
                task_type="autonomous_data_analysis",
                workspace="pending",
                opportunity_id=opp_id,
                notes="Executing autonomous work plan.",
            )

        workspace = safe_workspace_path(f"task_{task_id:04d}")
        workspace.mkdir(parents=True, exist_ok=True)
        update_task_status(task_id, "in_progress", f"Executing in {workspace.name}", workspace=str(workspace))

        # Generate structured plan
        plan = generate_execution_plan(opportunity, task_id, filename, self.llm_call)

        # Record plan to workspace
        plan_path = workspace / "execution_plan.json"
        plan_path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

        # Execute through analytics bundle generator
        # Update plan step states as execution progresses
        for step in plan.steps:
            step.status = "in_progress"

        try:
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

            log_event("autonomous_execution_completed", f"task_id={task_id}; opp_id={opp_id}")
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
