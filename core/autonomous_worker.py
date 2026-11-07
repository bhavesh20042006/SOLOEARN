from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.db import complete_task, get_opportunity, log_event, release_opportunity_execution_claim, update_opportunity_status, update_task_status
from core.execution_engine import AutonomousExecutionEngine, TaskPlan
from core.qa_agent import IndependentQAAgent, QAReport
from core.revision_engine import RevisionEngine


@dataclass
class AutonomousJobResult:
    task_id: int
    opportunity_id: int | None
    workspace: Path
    plan: TaskPlan
    initial_qa_report: QAReport
    final_qa_report: QAReport
    submission_ready: bool
    status: str  # "submission_ready" or "needs_manual_review"
    artifact_zip: Path
    revision_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "opportunity_id": self.opportunity_id,
            "workspace": str(self.workspace),
            "submission_ready": self.submission_ready,
            "status": self.status,
            "artifact_zip": str(self.artifact_zip),
            "qa_score_pct": self.final_qa_report.score_pct,
            "revision_count": self.revision_count,
        }


def run_autonomous_job(
    opportunity: dict[str, Any],
    file_bytes: bytes,
    filename: str,
    task_id: int | None = None,
    llm_call: Callable[[str, str], str] | None = None,
    max_revisions: int = 3,
) -> AutonomousJobResult:
    """Execute a job under an atomic per-opportunity lease, QA it, and release the lease."""
    opp_id = opportunity.get("id")
    if opp_id is not None:
        authoritative = get_opportunity(opp_id)
        if authoritative is None:
            raise ValueError(f"Opportunity #{opp_id} not found in database")
        if not authoritative.get("source_permitted"):
            raise ValueError(f"Opportunity #{opp_id} is from an unpermitted source and cannot be executed")
        if authoritative.get("status") not in {"approved_for_work", "in_progress"}:
            raise ValueError(
                f"Opportunity #{opp_id} is in status '{authoritative.get('status')}', not approved for work"
            )
        opportunity = authoritative
    else:
        if not opportunity.get("source_permitted", False):
            raise ValueError("Opportunity is from an unpermitted source and cannot be executed")
        if opportunity.get("status") not in {"approved_for_work", "in_progress"}:
            raise ValueError(f"Opportunity is not approved for work (status: '{opportunity.get('status')}')")

    engine: AutonomousExecutionEngine | None = None
    try:
        engine = AutonomousExecutionEngine(llm_call=llm_call)
        task_id, workspace, plan = engine.execute_job(
            opportunity=opportunity,
            file_bytes=file_bytes,
            filename=filename,
            task_id=task_id,
        )

        update_task_status(task_id, "qa_testing", "Running independent QA verification suite.", workspace=str(workspace))
        qa_agent = IndependentQAAgent(workspace, task_id)
        initial_qa = qa_agent.run_all_checks()
        final_qa = initial_qa
        revisions_run = 0

        if not initial_qa.passed:
            update_task_status(
                task_id,
                "revising",
                f"QA failed {len(initial_qa.failure_reasons)} checks; initiating revision engine.",
                workspace=str(workspace),
            )
            rev_engine = RevisionEngine(workspace, task_id, max_attempts=max_revisions)
            _, final_qa = rev_engine.repair_and_retest(initial_qa)
            revisions_run = len(rev_engine.history)

        zip_path = workspace / "deliverables.zip"
        if final_qa.passed:
            final_status = "submission_ready"
            update_task_status(
                task_id,
                "submission_ready",
                f"Independent QA passed ({final_qa.score_pct}%). Deliverables verified for client review.",
                artifact_path=str(zip_path),
                workspace=str(workspace),
            )
            if opp_id:
                update_opportunity_status(opp_id, "submission_ready")
        else:
            final_status = "needs_manual_review"
            update_task_status(
                task_id,
                "needs_manual_review",
                f"Independent QA flagged issues ({final_qa.score_pct}%). Requires manual review: {len(final_qa.failure_reasons)} failure(s).",
                artifact_path=str(zip_path),
                workspace=str(workspace),
            )
            if opp_id:
                update_opportunity_status(opp_id, "needs_manual_review")

        log_event(
            "autonomous_job_finished",
            f"task_id={task_id}; opp_id={opp_id}; status={final_status}; score={final_qa.score_pct}; revisions={revisions_run}",
        )
        return AutonomousJobResult(
            task_id=task_id,
            opportunity_id=opp_id,
            workspace=workspace,
            plan=plan,
            initial_qa_report=initial_qa,
            final_qa_report=final_qa,
            submission_ready=(final_status == "submission_ready"),
            status=final_status,
            artifact_zip=zip_path,
            revision_count=revisions_run,
        )
    except Exception:
        # Never mutate another worker's in-progress opportunity when our own
        # claim attempt failed. Only the process that acquired the lease may
        # move the record to manual review.
        if opp_id is not None and engine is not None and engine.claim_acquired:
            try:
                current = get_opportunity(opp_id)
                if current and current.get("status") == "in_progress":
                    update_opportunity_status(opp_id, "needs_manual_review")
            except Exception:
                pass
        raise
    finally:
        if opp_id is not None and engine is not None and engine.claim_acquired:
            release_opportunity_execution_claim(opp_id)
