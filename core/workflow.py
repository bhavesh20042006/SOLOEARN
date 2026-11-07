from __future__ import annotations

from pathlib import Path
from core.config import safe_workspace_path
from core.db import create_task


def create_csv_analysis_task(filename: str, opportunity_id: int | None = None) -> tuple[int, Path]:
    safe_name = Path(filename).name[:100]
    task_id_hint = "pending"
    workspace = safe_workspace_path(f"pending_{safe_name}")
    workspace.mkdir(parents=True, exist_ok=True)
    task_id = create_task(
        title=f"CSV business analysis: {safe_name}",
        task_type="csv_business_analysis",
        workspace=str(workspace.relative_to(safe_workspace_path())),
        opportunity_id=opportunity_id,
        notes="Created from the local workbench. External submission is not automated.",
    )
    # Use task-ID-scoped workspace from this point onward.
    final_workspace = safe_workspace_path(f"task_{task_id:04d}")
    final_workspace.mkdir(parents=True, exist_ok=True)
    return task_id, final_workspace
